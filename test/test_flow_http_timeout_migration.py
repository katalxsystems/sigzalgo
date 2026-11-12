"""Stored HTTP Request timeouts in seconds are migrated to milliseconds.

The node's timeout used to be read in seconds and is now read in
milliseconds. The run-time validator rejects anything under 1000 ms, so every
monitor created before the change (price-breach calls with 30/10, strategy
monitors with 15/10) failed with "timeout is in milliseconds and must be
between 1000 and 60000" and never ran. init_db rewrites them once.
"""

import pytest

from database import flow_db
from database.flow_db import FlowWorkflow, create_workflow, db_session, get_workflow, init_db
from services.flow_workflow_validator import validate_workflow


def _http(node_id, timeout):
    return {
        "id": node_id,
        "type": "httpRequest",
        "position": {"x": 0, "y": 100},
        "data": {"method": "POST", "url": "https://example.com/hook", "timeout": timeout},
    }


START = {
    "id": "start",
    "type": "start",
    "position": {"x": 0, "y": 0},
    "data": {"scheduleType": "manual"},
}


@pytest.fixture
def make():
    init_db()
    made = []

    def create(*http_nodes):
        nodes = [START, *http_nodes]
        edges = [{"id": f"e{n['id']}", "source": "start", "target": n["id"]} for n in http_nodes]
        workflow = create_workflow("timeout-mig-test", nodes=nodes, edges=edges)
        made.append(workflow.id)
        return workflow.id

    yield create
    FlowWorkflow.query.filter(FlowWorkflow.id.in_(made)).delete(synchronize_session=False)
    db_session.commit()
    db_session.remove()


def _timeouts(workflow_id):
    workflow = get_workflow(workflow_id)
    db_session.refresh(workflow)
    return {n["id"]: n["data"].get("timeout") for n in workflow.nodes if n["type"] == "httpRequest"}


def test_legacy_seconds_become_milliseconds_and_the_workflow_validates(make):
    wid = make(_http("notify", 30), _http("deactivate", 10), _http("string", "15"))
    flow_db._migrate_http_timeouts_to_ms()

    assert _timeouts(wid) == {"notify": 30000, "deactivate": 10000, "string": 15000}
    workflow = get_workflow(wid)
    assert (
        validate_workflow(
            {"name": workflow.name, "nodes": workflow.nodes, "edges": workflow.edges}, strict=True
        )
        == []
    )


def test_milliseconds_templates_and_unset_values_are_left_alone(make):
    wid = make(
        _http("ms", 15000),
        _http("template", "{{wait}}"),
        _http("zero", 0),
        _http("text", "soon"),
    )
    flow_db._migrate_http_timeouts_to_ms()

    assert _timeouts(wid) == {"ms": 15000, "template": "{{wait}}", "zero": 0, "text": "soon"}


def test_running_it_again_changes_nothing(make):
    wid = make(_http("notify", 30))
    flow_db._migrate_http_timeouts_to_ms()
    flow_db._migrate_http_timeouts_to_ms()
    assert _timeouts(wid) == {"notify": 30000}
