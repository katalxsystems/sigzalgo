# database/flow_db.py

import logging
import os
import secrets

from cachetools import TTLCache
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import func

logger = logging.getLogger(__name__)

# Flow workflow caches - 5 minute TTL for webhook lookups (high frequency)
_workflow_webhook_cache = TTLCache(maxsize=5000, ttl=300)  # 5 minutes TTL
_workflow_cache = TTLCache(maxsize=1000, ttl=600)  # 10 minutes TTL

DATABASE_URL = os.getenv("DATABASE_URL")

# Conditionally create engine based on DB type
if DATABASE_URL and "sqlite" in DATABASE_URL:
    # SQLite: Use NullPool to prevent connection pool exhaustion
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    # For other databases like PostgreSQL, use connection pooling
    engine = create_engine(DATABASE_URL, pool_size=50, max_overflow=100, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


def generate_webhook_token():
    """Generate a unique webhook token"""
    return secrets.token_urlsafe(32)


def generate_webhook_secret():
    """Generate a unique webhook secret for message validation"""
    return secrets.token_hex(32)


def get_workflow_api_key(workflow):
    """Decrypt and return a workflow's stored OpenAlgo API key.

    The api_key column transitioned from plaintext to Fernet-encrypted
    (auth_db Fernet, PBKDF2 over API_KEY_PEPPER). Pre-migration plaintext
    rows are returned as-is via safe_decrypt_token's fallback.
    """
    if not workflow or not workflow.api_key:
        return None
    from database.auth_db import safe_decrypt_token
    return safe_decrypt_token(workflow.api_key)


def _encrypt_api_key(api_key):
    """Encrypt an API key for storage in flow_workflows.api_key."""
    if not api_key:
        return None
    from database.auth_db import encrypt_token
    return encrypt_token(api_key)


class FlowWorkflow(Base):
    """Model for flow workflows"""

    __tablename__ = "flow_workflows"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    nodes = Column(JSON, default=list)
    edges = Column(JSON, default=list)
    is_active = Column(Boolean, default=False)
    schedule_job_id = Column(String(255), nullable=True)
    webhook_token = Column(String(64), unique=True, nullable=True, default=generate_webhook_token)
    webhook_secret = Column(String(64), nullable=True, default=generate_webhook_secret)
    webhook_enabled = Column(Boolean, default=False)
    webhook_auth_type = Column(String(20), default="payload")  # "payload" or "url"
    api_key = Column(
        String(255), nullable=True
    )  # Stored when workflow is activated, used for webhook execution
    # Ownership: the broker account (Auth.name) the workflow belongs to and
    # trades on, and the platform user owning that account. Users see and
    # manage only their active account's workflows; administrators see all.
    # NULL on workflows from before per-account Flow whose account could not
    # be determined -- visible to administrators only until one claims it.
    account_id = Column(String(255), nullable=True, index=True)
    owner_username = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    executions = relationship(
        "FlowWorkflowExecution", back_populates="workflow", cascade="all, delete-orphan"
    )


class PriceBreachCall(Base):
    """Maps a caller-supplied call_id (POST /api/v1/pricebreach/create) to the
    Flow workflow(s) it created.

    call_id has no other queryable home -- it is only embedded (non-unique)
    inside each workflow's notify-node JSON payload -- so
    /api/v1/pricebreach/<call_id>/deactivate needs this table to resolve
    which FlowWorkflow row(s) to tear down. entry_recross_workflow_id is
    nullable because create_and_activate() does not create that workflow
    when active_price == entry_price (see services/price_breach_service.py).
    """

    __tablename__ = "price_breach_calls"

    id = Column(Integer, primary_key=True, index=True)
    call_id = Column(String(100), nullable=False, index=True)
    sl_target_workflow_id = Column(Integer, ForeignKey("flow_workflows.id"), nullable=False)
    entry_recross_workflow_id = Column(Integer, ForeignKey("flow_workflows.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FlowWorkflowExecution(Base):
    """Model for flow workflow executions"""

    __tablename__ = "flow_workflow_executions"

    id = Column(Integer, primary_key=True, index=True)
    workflow_id = Column(Integer, ForeignKey("flow_workflows.id"), nullable=False)
    status = Column(String(50), default="pending")  # pending, running, completed, failed
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    logs = Column(JSON, default=list)
    error = Column(Text, nullable=True)

    # Relationships
    workflow = relationship("FlowWorkflow", back_populates="executions")


def init_db():
    """Initialize the database"""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Flow DB", logger)

    # Migrate: Add api_key column if it doesn't exist (for existing databases)
    _migrate_add_api_key_column()
    _migrate_add_ownership_columns()


def _migrate_add_ownership_columns():
    """Add account_id/owner_username to flow_workflows and backfill them.

    Idempotent. A workflow that was ever activated stores the OpenAlgo API key
    it runs with; that key belongs to exactly one broker account, which
    becomes the workflow's account. Workflows never activated have no key and
    stay unassigned (administrators only) until someone claims them.
    """
    try:
        from sqlalchemy import inspect, text

        inspector = inspect(engine)
        if "flow_workflows" not in inspector.get_table_names():
            return
        columns = {col["name"] for col in inspector.get_columns("flow_workflows")}
        for column in ("account_id", "owner_username"):
            if column not in columns:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE flow_workflows ADD COLUMN {column} VARCHAR(255)"))
                logger.info(f"Migration: Added '{column}' column to flow_workflows table")

        unassigned = FlowWorkflow.query.filter(
            FlowWorkflow.account_id.is_(None), FlowWorkflow.api_key.isnot(None)
        ).all()
        if not unassigned:
            return

        from database.auth_db import ApiKeys, decrypt_token, get_owner_username

        # plaintext key -> (account_id, owner) for every key on this install
        key_accounts = {}
        for row in ApiKeys.query.all():
            try:
                plaintext = decrypt_token(row.api_key_encrypted) if row.api_key_encrypted else None
            except Exception:
                plaintext = None
            if plaintext:
                account = row.account_id or row.user_id
                key_accounts[plaintext] = (account, get_owner_username(account) or row.user_id)

        assigned = 0
        for workflow in unassigned:
            match = key_accounts.get(get_workflow_api_key(workflow))
            if match:
                workflow.account_id, workflow.owner_username = match
                assigned += 1
        db_session.commit()
        _workflow_cache.clear()
        logger.info(
            f"Migration: assigned {assigned} of {len(unassigned)} workflow(s) to the "
            "account behind their stored API key"
        )
    except Exception:
        db_session.rollback()
        logger.exception("Migration of flow_workflows ownership columns failed")


def _migrate_add_api_key_column():
    """Add api_key column to flow_workflows table if it doesn't exist"""
    try:
        from sqlalchemy import inspect, text

        inspector = inspect(engine)

        # Check if table exists
        if "flow_workflows" not in inspector.get_table_names():
            return

        # Check if column exists
        columns = [col["name"] for col in inspector.get_columns("flow_workflows")]
        if "api_key" not in columns:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE flow_workflows ADD COLUMN api_key VARCHAR(255)"))
                conn.commit()
                logger.info("Migration: Added 'api_key' column to flow_workflows table")
    except Exception as e:
        # Log but don't fail - column might already exist or other DB issue
        logger.debug(f"Migration check for api_key column: {e}")


# --- Workflow CRUD Operations ---


def create_workflow(
    name, description=None, nodes=None, edges=None, account_id=None, owner_username=None
):
    """Create a new workflow owned by ``account_id`` / ``owner_username``"""
    try:
        workflow = FlowWorkflow(
            name=name,
            description=description,
            nodes=nodes or [],
            edges=edges or [],
            account_id=account_id,
            owner_username=owner_username,
        )
        db_session.add(workflow)
        db_session.commit()

        # Clear workflow cache
        _workflow_cache.clear()

        logger.info(f"Created workflow: {name} (id={workflow.id})")
        return workflow
    except Exception as e:
        logger.exception(f"Error creating workflow: {str(e)}")
        db_session.rollback()
        return None


def get_workflow(workflow_id):
    """Get workflow by ID"""
    try:
        return FlowWorkflow.query.get(workflow_id)
    except Exception as e:
        logger.exception(f"Error getting workflow {workflow_id}: {str(e)}")
        return None


def get_workflow_by_webhook_token(webhook_token):
    """Get workflow by webhook token (cached for 5 minutes)"""
    # Check cache first
    if webhook_token in _workflow_webhook_cache:
        return _workflow_webhook_cache[webhook_token]

    try:
        workflow = FlowWorkflow.query.filter_by(webhook_token=webhook_token).first()
        # Cache the result (including None for not found)
        if workflow:
            _workflow_webhook_cache[webhook_token] = workflow
        return workflow
    except Exception as e:
        logger.exception(f"Error getting workflow by webhook token: {str(e)}")
        return None


def get_workflows_for_account(account_id):
    """Workflows belonging to one broker account"""
    try:
        return (
            FlowWorkflow.query.filter_by(account_id=account_id)
            .order_by(FlowWorkflow.updated_at.desc())
            .all()
        )
    except Exception as e:
        logger.exception(f"Error getting workflows for account {account_id}: {str(e)}")
        return []


def owner_for_api_key(api_key):
    """(account_id, owner_username) an OpenAlgo API key belongs to, or
    (None, None). For creators that act with a caller's key (the price-breach
    API, the agent) rather than a browser session."""
    if not api_key:
        return None, None
    from database.auth_db import get_owner_username, verify_api_key

    account_id = verify_api_key(api_key)
    if not account_id:
        return None, None
    return account_id, get_owner_username(account_id)


def set_workflow_owner(workflow_id, account_id, owner_username):
    """Assign a workflow to a broker account (claiming an unassigned one)"""
    try:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return None
        workflow.account_id = account_id
        workflow.owner_username = owner_username
        db_session.commit()
        _workflow_cache.clear()
        if workflow.webhook_token in _workflow_webhook_cache:
            del _workflow_webhook_cache[workflow.webhook_token]
        logger.info(f"Workflow {workflow_id} assigned to account {account_id}")
        return workflow
    except Exception as e:
        logger.exception(f"Error assigning workflow {workflow_id}: {str(e)}")
        db_session.rollback()
        return None


def get_all_workflows():
    """Get all workflows"""
    try:
        return FlowWorkflow.query.order_by(FlowWorkflow.updated_at.desc()).all()
    except Exception as e:
        logger.exception(f"Error getting all workflows: {str(e)}")
        return []


def get_active_workflows():
    """Get all active workflows"""
    try:
        return FlowWorkflow.query.filter_by(is_active=True).all()
    except Exception as e:
        logger.exception(f"Error getting active workflows: {str(e)}")
        return []


def record_price_breach_call(call_id, sl_target_workflow_id, entry_recross_workflow_id=None):
    """Persist the call_id -> workflow_id(s) mapping for one
    /api/v1/pricebreach/create call, so it can later be resolved by
    get_workflow_ids_by_call_id()."""
    try:
        row = PriceBreachCall(
            call_id=call_id,
            sl_target_workflow_id=sl_target_workflow_id,
            entry_recross_workflow_id=entry_recross_workflow_id,
        )
        db_session.add(row)
        db_session.commit()
        return row
    except Exception as e:
        logger.exception(f"Error recording price-breach call {call_id}: {str(e)}")
        db_session.rollback()
        return None


def get_workflow_ids_by_call_id(call_id):
    """All FlowWorkflow ids ever created for this call_id (sl_target and, if
    present, entry_recross), across every /create call that used it. Most
    calls will resolve to exactly one row (one or two workflow ids); more
    than one row only happens if the same call_id was reused across
    multiple /create calls."""
    try:
        rows = PriceBreachCall.query.filter_by(call_id=call_id).all()
        workflow_ids: list[int] = []
        for row in rows:
            workflow_ids.append(row.sl_target_workflow_id)
            if row.entry_recross_workflow_id is not None:
                workflow_ids.append(row.entry_recross_workflow_id)
        return workflow_ids
    except Exception as e:
        logger.exception(f"Error looking up workflows for call_id {call_id}: {str(e)}")
        return []


def update_workflow(workflow_id, **kwargs):
    """Update workflow fields"""
    try:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return None

        # Update allowed fields
        allowed_fields = [
            "name",
            "description",
            "nodes",
            "edges",
            "is_active",
            "schedule_job_id",
            "webhook_enabled",
            "webhook_auth_type",
            "api_key",
        ]
        for field in allowed_fields:
            if field in kwargs:
                # api_key is encrypted at rest with the auth_db Fernet.
                if field == "api_key":
                    setattr(workflow, field, _encrypt_api_key(kwargs[field]))
                else:
                    setattr(workflow, field, kwargs[field])

        db_session.commit()

        # Clear caches
        _workflow_cache.clear()
        if workflow.webhook_token in _workflow_webhook_cache:
            del _workflow_webhook_cache[workflow.webhook_token]

        logger.info(f"Updated workflow {workflow_id}")
        return workflow
    except Exception as e:
        logger.exception(f"Error updating workflow {workflow_id}: {str(e)}")
        db_session.rollback()
        return None


def delete_workflow(workflow_id):
    """Delete workflow and its executions"""
    try:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return False

        # Store for cache invalidation
        webhook_token = workflow.webhook_token

        db_session.delete(workflow)
        db_session.commit()

        # Clear caches
        _workflow_cache.clear()
        if webhook_token in _workflow_webhook_cache:
            del _workflow_webhook_cache[webhook_token]

        logger.info(f"Deleted workflow {workflow_id}")
        return True
    except Exception as e:
        logger.exception(f"Error deleting workflow {workflow_id}: {str(e)}")
        db_session.rollback()
        return False


def activate_workflow(workflow_id, api_key=None):
    """Activate a workflow and optionally store the API key for webhook execution"""
    kwargs = {"is_active": True}
    if api_key:
        kwargs["api_key"] = api_key
    return update_workflow(workflow_id, **kwargs)


def deactivate_workflow(workflow_id):
    """Deactivate a workflow"""
    return update_workflow(workflow_id, is_active=False)


def regenerate_webhook_token(workflow_id):
    """Regenerate webhook token for a workflow"""
    try:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return None

        old_token = workflow.webhook_token
        workflow.webhook_token = generate_webhook_token()
        db_session.commit()

        # Clear old token from cache
        if old_token in _workflow_webhook_cache:
            del _workflow_webhook_cache[old_token]

        logger.info(f"Regenerated webhook token for workflow {workflow_id}")
        return workflow.webhook_token
    except Exception as e:
        logger.exception(f"Error regenerating webhook token for workflow {workflow_id}: {str(e)}")
        db_session.rollback()
        return None


def regenerate_webhook_secret(workflow_id):
    """Regenerate webhook secret for a workflow"""
    try:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return None

        workflow.webhook_secret = generate_webhook_secret()
        db_session.commit()

        logger.info(f"Regenerated webhook secret for workflow {workflow_id}")
        return workflow.webhook_secret
    except Exception as e:
        logger.exception(f"Error regenerating webhook secret for workflow {workflow_id}: {str(e)}")
        db_session.rollback()
        return None


def enable_webhook(workflow_id):
    """Enable webhook for a workflow"""
    return update_workflow(workflow_id, webhook_enabled=True)


def disable_webhook(workflow_id):
    """Disable webhook for a workflow"""
    return update_workflow(workflow_id, webhook_enabled=False)


def set_webhook_auth_type(workflow_id, auth_type):
    """Set webhook auth type for a workflow"""
    if auth_type not in ["payload", "url"]:
        logger.error(f"Invalid webhook auth type: {auth_type}")
        return None
    return update_workflow(workflow_id, webhook_auth_type=auth_type)


def ensure_webhook_credentials(workflow_id):
    """Ensure webhook token and secret exist for a workflow"""
    try:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return False

        needs_update = False
        if not workflow.webhook_token:
            workflow.webhook_token = generate_webhook_token()
            needs_update = True
        if not workflow.webhook_secret:
            workflow.webhook_secret = generate_webhook_secret()
            needs_update = True

        if needs_update:
            db_session.commit()
            # Clear cache to force refresh
            _workflow_cache.clear()
            logger.info(f"Generated webhook credentials for workflow {workflow_id}")

        return True
    except Exception as e:
        logger.exception(f"Error ensuring webhook credentials for workflow {workflow_id}: {str(e)}")
        db_session.rollback()
        return False


def set_schedule_job_id(workflow_id, job_id):
    """Set schedule job ID for a workflow"""
    try:
        workflow = get_workflow(workflow_id)
        if not workflow:
            return None

        workflow.schedule_job_id = job_id
        db_session.commit()

        logger.info(f"Set schedule job ID {job_id} for workflow {workflow_id}")
        return workflow
    except Exception as e:
        logger.exception(f"Error setting schedule job ID for workflow {workflow_id}: {str(e)}")
        db_session.rollback()
        return None


# --- Workflow Execution CRUD Operations ---


def create_execution(workflow_id, status="pending"):
    """Create a new workflow execution"""
    try:
        execution = FlowWorkflowExecution(
            workflow_id=workflow_id,
            status=status,
            logs=[],
            # Executions are created already "running"; update_execution_status
            # only stamps started_at on a later change *to* running, so without
            # this every run had no start time.
            started_at=func.now() if status == "running" else None,
        )
        db_session.add(execution)
        db_session.commit()

        logger.info(f"Created execution for workflow {workflow_id} (id={execution.id})")
        return execution
    except Exception as e:
        logger.exception(f"Error creating execution for workflow {workflow_id}: {str(e)}")
        db_session.rollback()
        return None


def get_execution(execution_id):
    """Get execution by ID"""
    try:
        return FlowWorkflowExecution.query.get(execution_id)
    except Exception as e:
        logger.exception(f"Error getting execution {execution_id}: {str(e)}")
        return None


def get_workflow_executions(workflow_id, limit=50):
    """Get executions for a workflow"""
    try:
        return (
            FlowWorkflowExecution.query.filter_by(workflow_id=workflow_id)
            # Newest first by id: older rows have no started_at (see
            # create_execution), and NULLs sort differently per backend.
            .order_by(FlowWorkflowExecution.id.desc())
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.exception(f"Error getting executions for workflow {workflow_id}: {str(e)}")
        return []


# Most log entries kept per execution: long-running monitor workflows can log
# every few seconds for hours.
MAX_EXECUTION_LOG_ENTRIES = 1000


def update_execution_status(execution_id, status, error=None, logs=None):
    """Update execution status, and when ``logs`` is given, store the run's log
    (the last MAX_EXECUTION_LOG_ENTRIES entries) so it can be viewed later --
    scheduled, webhook, price-alert and order-update runs have no HTTP
    response to carry it."""
    try:
        execution = get_execution(execution_id)
        if not execution:
            return None

        execution.status = status
        if error:
            execution.error = error
        if logs is not None:
            execution.logs = list(logs[-MAX_EXECUTION_LOG_ENTRIES:])

        if status == "running" and not execution.started_at:
            execution.started_at = func.now()
        elif status in ["completed", "failed"]:
            execution.completed_at = func.now()

        db_session.commit()

        logger.info(f"Updated execution {execution_id} status to {status}")
        return execution
    except Exception as e:
        logger.exception(f"Error updating execution {execution_id}: {str(e)}")
        db_session.rollback()
        return None


def add_execution_log(execution_id, log_entry):
    """Add a log entry to execution"""
    try:
        execution = get_execution(execution_id)
        if not execution:
            return None

        # Get current logs and append
        logs = execution.logs or []
        logs.append(log_entry)
        execution.logs = logs

        db_session.commit()
        return execution
    except Exception as e:
        logger.exception(f"Error adding log to execution {execution_id}: {str(e)}")
        db_session.rollback()
        return None


def clear_workflow_cache():
    """Clear all workflow caches"""
    _workflow_webhook_cache.clear()
    _workflow_cache.clear()
    logger.info("Flow workflow cache cleared")
