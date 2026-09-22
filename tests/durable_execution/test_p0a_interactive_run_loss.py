"""P0-A BASELINE CHARACTERIZATION (SOURCE-LEVEL / COMPONENT) — interactive task loss on /v1/runs.

NOTE: this asserts the CURRENT (defective) behavior at the component level
(adapter reinstantiation models a process restart). A full transport/process
restart E2E is P0-H. Do NOT claim Web/Electron durability from this alone.

Durable-execution requirement: an accepted task must survive a backend restart;
its status, events and final result must remain retrievable by a durable id.

This reproduction proves the CURRENT behavior on origin/main (0.19.1): /v1/runs
state lives only in per-instance in-memory dicts, so a backend restart (modeled
as a fresh APIServerAdapter — __init__ re-creates the dicts empty) loses the
accepted run entirely, while the /v1/responses surface persists to SQLite.

Ownership note: this stream owns durable task/run identity. The fix is NOT here
yet — this test documents the defect. It currently PASSES because it asserts the
broken behavior; when durable /v1/runs lands it must be inverted.
"""

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def test_v1_runs_state_is_in_memory_only():
    """A run's status is only ever a dict entry — no disk row is written."""
    adapter = _make_adapter()
    run_id = "run_p0a_demo"

    adapter._set_run_status(run_id, "completed", output="ENTERPRISE_RESULT_42")

    # Present in-process...
    assert run_id in adapter._run_statuses
    assert adapter._run_statuses[run_id]["status"] == "completed"
    assert adapter._run_statuses[run_id]["output"] == "ENTERPRISE_RESULT_42"

    # ...but the store is a plain dict, not a durable handle.
    assert isinstance(adapter._run_statuses, dict)
    assert isinstance(adapter._run_streams, dict)


def test_v1_runs_lost_on_restart():
    """Modelled restart: a fresh adapter has zero knowledge of the accepted run.

    This is the precise interactive-task-loss defect [C-WEB-1]: after a backend
    restart the accepted enterprise task and its final result are gone.
    """
    before = _make_adapter()
    run_id = "run_p0a_restart"
    before._set_run_status(run_id, "completed", output="MUST_SURVIVE_RESTART")
    assert before._run_statuses.get(run_id, {}).get("status") == "completed"

    # Backend restart: a new process re-runs __init__, re-creating empty dicts.
    after = _make_adapter()

    assert after._run_statuses == {}, "fresh adapter must start empty"
    assert run_id not in after._run_statuses, (
        "DEFECT REPRODUCED: accepted /v1/runs task is lost after restart — "
        "no durable task row, events or final result survive"
    )
    # The final result is unrecoverable by its durable id after restart.
    assert after._run_statuses.get(run_id) is None


def test_v1_responses_is_the_durable_contrast():
    """The /v1/responses surface DOES persist (SQLite ResponseStore) — proving the
    gap is specific to /v1/runs, and a durable pattern already exists to mirror."""
    import inspect

    from gateway.platforms import api_server as mod

    src = inspect.getsource(mod)
    # ResponseStore is backed by an on-disk sqlite file; /v1/runs has no equivalent.
    assert "ResponseStore" in src
    assert "response_store.db" in src
    # /v1/runs status/stream stores are the in-memory dicts asserted above.
    assert "_run_statuses" in src and "_run_streams" in src
