"""Owner item 6 — the canonical workspace-scoped receipt lookup fails closed
across workspaces even for the SAME tenant + user.

A managed caller in workspace B must never read, list or otherwise observe a
receipt created in workspace A. ``get_effect_in_workspace`` /
``list_effects_in_workspace`` return not-found (never metadata) for a foreign
workspace, while the principal-only ``get_effect`` still sees it (which is
exactly why managed APIs must use the workspace-scoped variant)."""

from __future__ import annotations

from youtab_runtime import effect_ledger as ledger
from youtab_runtime.run_journal import Principal


def _seed(db_path, *, run_id, workspace):
    principal = Principal("tenant-alpha", "user-eiman")
    rec = ledger.begin_effect(
        run_id, principal, "fs.write",
        {"ws": workspace, "path": f"/w/{workspace}/f.txt", "content": "0" * 64},
        correlation_id="grant-1", detail={"workspace": workspace},
        db_path=db_path,
    )
    return principal, rec


def test_get_effect_in_workspace_hides_foreign_workspace(tmp_path):
    db = tmp_path / "effects_ws.db"
    principal, rec = _seed(db, run_id="run-A", workspace="ws-A")

    # Same principal, correct workspace -> visible.
    got = ledger.get_effect_in_workspace(rec.effect_id, principal, "ws-A", db_path=db)
    assert got is not None and got.effect_id == rec.effect_id

    # Same tenant + user, DIFFERENT workspace -> not-found (fail closed), no metadata.
    assert ledger.get_effect_in_workspace(rec.effect_id, principal, "ws-B", db_path=db) is None

    # The principal-only lookup still sees it — proving the scoping is the
    # workspace check, not principal ownership.
    assert ledger.get_effect(rec.effect_id, principal, db_path=db) is not None


def test_list_effects_in_workspace_filters_foreign(tmp_path):
    db = tmp_path / "effects_ws_list.db"
    principal, rec_a = _seed(db, run_id="run-shared", workspace="ws-A")
    # A second effect on the SAME run + principal but a different workspace.
    ledger.begin_effect(
        "run-shared", principal, "fs.write",
        {"ws": "ws-B", "path": "/w/ws-B/g.txt", "content": "1" * 64},
        correlation_id="grant-2", detail={"workspace": "ws-B"},
        db_path=db,
    )

    ws_a = ledger.list_effects_in_workspace("run-shared", principal, "ws-A", db_path=db)
    assert [r.effect_id for r in ws_a] == [rec_a.effect_id]

    # The principal-only listing sees BOTH; the workspace-scoped one sees ONE.
    assert len(ledger.list_effects("run-shared", principal, db_path=db)) == 2
