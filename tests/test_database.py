"""SQLite persistence and restart recovery."""

from __future__ import annotations

from orchestrator.models import TaskStatus
from orchestrator.storage.database import Database, DatabaseError
from orchestrator.models import Task


def _task(key: str = "k1", objective: str = "obj") -> Task:
    return Task(
        id=f"task-{key}",
        idempotency_key=f"idem-{key}",
        project="DRIFTLINE",
        branch="driftline/x",
        objective=objective,
        acceptance_criteria=["a", "b"],
    )


def test_persist_and_reload(base_dir):
    path = base_dir / "data" / "persist.db"
    db = Database(path)
    task, _ = None, None
    created = _task()
    db.create_task(created)
    db.update_task(
        created.id,
        status=TaskStatus.RUNNING.value,
        arena_ref="ref-1",
        changed_files=["src/a.lua"],
        test_results={"passed": 3},
    )
    db.close()

    reopened = Database(path)
    loaded = reopened.get_task(created.id)
    assert loaded is not None
    assert loaded.status == TaskStatus.RUNNING.value
    assert loaded.arena_ref == "ref-1"
    assert loaded.changed_files == ["src/a.lua"]
    assert loaded.test_results == {"passed": 3}
    assert loaded.acceptance_criteria == ["a", "b"]
    reopened.close()


def test_wal_mode_enabled(base_dir):
    db = Database(base_dir / "data" / "wal.db")
    mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    db.close()


def test_duplicate_idempotency_key_rejected(base_dir):
    db = Database(base_dir / "data" / "dup.db")
    db.create_task(_task("dup"))
    try:
        db.create_task(_task("dup"))
        raised = False
    except DatabaseError:
        raised = True
    assert raised
    db.close()


def test_status_change_creates_audit_event(base_dir):
    db = Database(base_dir / "data" / "events.db")
    task = _task("ev")
    db.create_task(task)
    db.update_task(task.id, status=TaskStatus.DISPATCH_PENDING.value)
    db.update_task(task.id, status=TaskStatus.RUNNING.value)
    events = db.events(task.id)
    assert [e["to_status"] for e in events] == [
        TaskStatus.READY.value,
        TaskStatus.DISPATCH_PENDING.value,
        TaskStatus.RUNNING.value,
    ]
    db.close()


def test_approvals_lifecycle(base_dir):
    db = Database(base_dir / "data" / "appr.db")
    approval_id = db.request_approval("merge_branch", "needs human")
    assert db.get_approval(approval_id)["status"] == "PENDING"
    assert len(db.pending_approvals()) == 1
    db.resolve_approval(approval_id, granted=False)
    assert db.get_approval(approval_id)["status"] == "DENIED"
    assert db.pending_approvals() == []
    db.close()


def test_idempotency_log(base_dir):
    db = Database(base_dir / "data" / "idem.db")
    db.record_idempotency("idem-x", "arena_dispatch", "arena-1")
    assert db.idempotency_response("idem-x") == "arena-1"
    db.record_idempotency("idem-x", "arena_dispatch", "arena-2")
    assert db.idempotency_response("idem-x") == "arena-1"  # first write wins
    db.close()


def test_count_by_status(base_dir):
    db = Database(base_dir / "data" / "counts.db")
    db.create_task(_task("a", "a"))
    db.create_task(_task("b", "b"))
    db.update_task("task-a", status=TaskStatus.RUNNING.value)
    counts = db.count_by_status()
    assert counts[TaskStatus.READY.value] == 1
    assert counts[TaskStatus.RUNNING.value] == 1
    db.close()
