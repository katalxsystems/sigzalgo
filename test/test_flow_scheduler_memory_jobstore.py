"""The Flow scheduler keeps its jobs in memory, never in openalgo.db.

With a database job store, APScheduler's own thread wrote each job's next run
time to openalgo.db after every run. When another writer held the lock past the
retry budget (a master contract download does), that write raised inside the
scheduler loop, the thread died with "Exception in thread APScheduler", and no
scheduled workflow ran again until a restart. The schedule is rebuilt from the
active workflows at every boot (reconcile_scheduler_jobs), so nothing needs to
be persisted.
"""

import sqlite3

import pytest
from apscheduler.jobstores.memory import MemoryJobStore

import services.flow_scheduler_service as sched


@pytest.fixture
def fresh_scheduler(monkeypatch):
    """A FlowScheduler of its own, not the process singleton."""
    monkeypatch.setattr(sched.FlowScheduler, "_instance", None)
    monkeypatch.setattr(sched.FlowScheduler, "_scheduler", None)
    monkeypatch.setattr(sched.FlowScheduler, "_initialized", False)
    scheduler = sched.FlowScheduler()
    scheduler.init()
    yield scheduler
    scheduler.shutdown()


def test_jobs_are_held_in_memory(fresh_scheduler):
    jobstores = fresh_scheduler.scheduler._jobstores

    assert set(jobstores) == {"default"}
    assert isinstance(jobstores["default"], MemoryJobStore)


def test_scheduling_a_workflow_never_opens_a_database_connection(fresh_scheduler, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the Flow scheduler opened a database connection")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", refuse)

    job_id = fresh_scheduler.add_workflow_job(
        workflow_id=901, schedule_type="interval", interval_value=5, interval_unit="minutes"
    )

    assert fresh_scheduler.get_next_run_time(job_id) is not None
    assert fresh_scheduler.remove_workflow_job(901) is True
