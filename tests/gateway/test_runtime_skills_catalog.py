"""The service-plane skills catalog exposes the real installed-skills registry.

`GET /api/runtime/v1/skills` used to import a non-existent ``skills_registry``
module and always returned ``{"skills": [], "available": false}`` — the product
Skills surface was blank. The catalog now reads the same authoritative finder the
dashboard ``/api/skills`` route and the ``youtab skills`` CLI use
(``tools.skills_tool._find_all_skills``). These tests pin the transform and the
fail-soft contract without depending on what is installed on disk.
"""

from __future__ import annotations

from youtab_agent_cli.web_routers import runtime


def test_skills_catalog_uses_real_finder(monkeypatch):
    monkeypatch.setattr(
        "tools.skills_tool._find_all_skills",
        lambda *a, **k: [
            {"name": "b-skill", "description": "B", "category": "z"},
            {"name": "a-skill", "description": "A", "category": "z"},
            {"name": "", "description": "skip", "category": "z"},
        ],
    )

    out = runtime._skills_catalog()

    assert out["available"] is True
    # Empty-name entry dropped; sorted by (category, name) via the real _sort_skills.
    assert [s["id"] for s in out["skills"]] == ["a-skill", "b-skill"]
    first = out["skills"][0]
    assert first["id"] == first["name"] == "a-skill"
    assert first["description"] == "A"
    assert first["category"] == "z"


def test_skills_catalog_fails_soft_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("scan failed")

    monkeypatch.setattr("tools.skills_tool._find_all_skills", boom)

    out = runtime._skills_catalog()

    assert out["available"] is False
    assert out["reason"] == "skills_catalog_unavailable"
    assert out["skills"] == []
