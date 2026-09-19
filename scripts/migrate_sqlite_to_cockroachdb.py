"""One-time data migration: copy rows from the existing SQLite database
files into a target CockroachDB cluster.

Mapping (see the SQLite -> CockroachDB migration plan):

    openalgo database <- db/openalgo.db, db/logs.db, db/latency.db, db/health.db
    sandbox  database <- db/sandbox.db
    (db/historify.duckdb is untouched -- separate DuckDB engine, out of scope)

APScheduler job-store tables (flow_apscheduler_jobs, historify's job store) are
deliberately excluded: they hold pickled in-process job state that is
reconciled fresh on every app startup (see services/flow_scheduler_service.py
and app.py's ensure_jobstore_tables_exist()/reconcile_scheduler_jobs()), so
carrying old pickled rows across a Python/library version boundary is more
likely to crash a job than help.

Schema is created on the target via each module's own SQLAlchemy metadata
(Base.metadata.create_all), the same dialect-agnostic mechanism app.py uses
at normal startup -- so this script only needs to copy rows, not DDL.

Usage:
    uv run --group cockroachdb python scripts/migrate_sqlite_to_cockroachdb.py \\
        --openalgo-target "cockroachdb://user:pass@host:26257/openalgo?sslmode=verify-full" \\
        --sandbox-target  "cockroachdb://user:pass@host:26257/sandbox?sslmode=verify-full" \\
        --execute

Without --execute the script only reports source row counts and target
reachability (a dry run). Add --truncate-target to delete existing rows in a
target table before copying -- refused by default so a re-run can't silently
duplicate rows. Add --tables/--skip-tables (comma-separated table names) to
restrict the run.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, inspect, select, text  # noqa: E402

from database.engine_factory import create_db_engine  # noqa: E402
from utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)


@dataclass
class DbModule:
    module_path: str
    target: str  # "openalgo" or "sandbox"
    base_attr: str = "Base"
    engine_attr: str = "engine"


MODULES: list[DbModule] = [
    DbModule("database.action_center_db", "openalgo"),
    DbModule("database.analyzer_db", "openalgo"),
    DbModule("database.apilog_db", "openalgo"),
    DbModule("database.auth_db", "openalgo"),
    DbModule("database.chart_prefs_db", "openalgo"),
    DbModule("database.chartink_db", "openalgo"),
    DbModule("database.flow_db", "openalgo"),
    DbModule("database.leverage_db", "openalgo"),
    DbModule("database.market_calendar_db", "openalgo"),
    DbModule("database.master_contract_status_db", "openalgo"),
    DbModule("database.oauth_db", "openalgo"),
    DbModule("database.qty_freeze_db", "openalgo"),
    DbModule("database.settings_db", "openalgo"),
    DbModule("database.strategy_db", "openalgo"),
    DbModule("database.strategy_portfolio_db", "openalgo"),
    DbModule("database.symbol", "openalgo"),
    DbModule("database.telegram_db", "openalgo"),
    DbModule("database.user_db", "openalgo"),
    DbModule("database.whatsapp_db", "openalgo"),
    DbModule("database.agent_db", "openalgo"),
    DbModule("database.strategy_module_db", "openalgo"),
    DbModule("database.strategy_book_db", "openalgo"),
    DbModule("database.traffic_db", "openalgo", base_attr="LogBase", engine_attr="logs_engine"),
    DbModule("database.latency_db", "openalgo", base_attr="LatencyBase", engine_attr="latency_engine"),
    DbModule("database.health_db", "openalgo", base_attr="HealthBase", engine_attr="health_engine"),
    DbModule("database.sandbox_db", "sandbox"),
]

BATCH_SIZE = 1000


@dataclass
class TableResult:
    module: str
    table: str
    source_rows: int
    copied: int = 0
    status: str = "pending"
    detail: str = ""


def _reset_sequences(target_engine, base) -> None:
    """Advance CockroachDB/Postgres integer PK sequences past imported values.

    A no-op on SQLite targets. Explicit-value INSERTs (what this script does)
    never touch a column's server-side default, so a sequence-backed PK left
    at its start value would collide with imported rows on the next organic
    insert. Silently skips columns that are not sequence-backed (e.g. string
    PKs, or CockroachDB's own unique_rowid() default) -- pg_get_serial_sequence
    returns NULL for those and the setval call is expected to fail.
    """
    if target_engine.url.get_backend_name() == "sqlite":
        return
    with target_engine.begin() as conn:
        for table in base.metadata.sorted_tables:
            for col in table.primary_key.columns:
                max_val = conn.execute(select(func.max(col))).scalar()
                if max_val is None:
                    continue
                try:
                    conn.execute(
                        text("SELECT setval(pg_get_serial_sequence(:t, :c), :v)"),
                        {"t": table.name, "c": col.name, "v": max_val},
                    )
                except Exception:
                    logger.debug(
                        f"{table.name}.{col.name}: not sequence-backed, skipping setval"
                    )


def _copy_table(
    source_engine,
    target_engine,
    table,
    *,
    execute: bool,
    truncate_target: bool,
) -> TableResult:
    result = TableResult(module=table.fullname, table=table.name, source_rows=0)

    inspector = inspect(source_engine)
    if not inspector.has_table(table.name):
        result.status = "skipped"
        result.detail = "source table does not exist"
        return result

    with source_engine.connect() as src_conn:
        result.source_rows = src_conn.execute(
            select(func.count()).select_from(table)
        ).scalar_one()

    if not execute:
        result.status = "dry-run"
        return result

    with target_engine.begin() as tgt_conn:
        existing = tgt_conn.execute(select(func.count()).select_from(table)).scalar_one()
        if existing and truncate_target:
            tgt_conn.execute(table.delete())
        elif existing:
            result.status = "failed"
            result.detail = f"target already has {existing} row(s); use --truncate-target"
            return result

    if result.source_rows == 0:
        result.status = "ok"
        return result

    with source_engine.connect() as src_conn:
        cursor_result = src_conn.execute(select(table))
        while True:
            batch = cursor_result.fetchmany(BATCH_SIZE)
            if not batch:
                break
            rows = [dict(row._mapping) for row in batch]
            with target_engine.begin() as tgt_conn:
                tgt_conn.execute(table.insert(), rows)
            result.copied += len(rows)

    result.status = "ok" if result.copied == result.source_rows else "mismatch"
    return result


def run(
    targets: dict[str, str | None],
    *,
    execute: bool,
    truncate_target: bool,
    only_tables: set[str] | None,
    skip_tables: set[str] | None,
) -> list[TableResult]:
    results: list[TableResult] = []
    target_engines: dict[str, object] = {}

    for mod_def in MODULES:
        target_url = targets.get(mod_def.target)
        if not target_url:
            logger.warning(
                f"{mod_def.module_path}: no --{mod_def.target}-target given, skipping"
            )
            continue

        module = importlib.import_module(mod_def.module_path)
        base = getattr(module, mod_def.base_attr)
        source_engine = getattr(module, mod_def.engine_attr)

        if mod_def.target not in target_engines:
            target_engines[mod_def.target] = create_db_engine(target_url)
        target_engine = target_engines[mod_def.target]

        if execute:
            base.metadata.create_all(bind=target_engine)

        for table in base.metadata.sorted_tables:
            if only_tables and table.name not in only_tables:
                continue
            if skip_tables and table.name in skip_tables:
                continue
            res = _copy_table(
                source_engine,
                target_engine,
                table,
                execute=execute,
                truncate_target=truncate_target,
            )
            res.module = mod_def.module_path
            results.append(res)
            logger.info(
                f"{mod_def.module_path}.{table.name}: {res.status} "
                f"(source={res.source_rows}, copied={res.copied}) {res.detail}"
            )

        if execute:
            _reset_sequences(target_engine, base)

    for engine in target_engines.values():
        engine.dispose()

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openalgo-target", help="CockroachDB URL for the merged openalgo database")
    parser.add_argument("--sandbox-target", help="CockroachDB URL for the sandbox database")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually copy rows. Without this flag, only reports source row counts.",
    )
    parser.add_argument(
        "--truncate-target",
        action="store_true",
        help="Delete existing rows in a target table before copying. Refused by default.",
    )
    parser.add_argument("--tables", help="Comma-separated table names to restrict to")
    parser.add_argument("--skip-tables", help="Comma-separated table names to exclude")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    only_tables = set(args.tables.split(",")) if args.tables else None
    skip_tables = set(args.skip_tables.split(",")) if args.skip_tables else None

    targets = {"openalgo": args.openalgo_target, "sandbox": args.sandbox_target}
    if not targets["openalgo"] and not targets["sandbox"]:
        logger.error("Pass at least one of --openalgo-target / --sandbox-target")
        return 1

    results = run(
        targets,
        execute=args.execute,
        truncate_target=args.truncate_target,
        only_tables=only_tables,
        skip_tables=skip_tables,
    )

    failed = [r for r in results if r.status in ("failed", "mismatch")]
    total_source = sum(r.source_rows for r in results)
    total_copied = sum(r.copied for r in results)

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"\n=== {mode} summary: {len(results)} table(s), {total_source} source row(s), {total_copied} copied ===")
    for r in results:
        line = f"  {r.module}.{r.table}: {r.status} source={r.source_rows} copied={r.copied}"
        if r.detail:
            line += f" -- {r.detail}"
        print(line)

    if failed:
        print(f"\n{len(failed)} table(s) failed or had a row-count mismatch.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
