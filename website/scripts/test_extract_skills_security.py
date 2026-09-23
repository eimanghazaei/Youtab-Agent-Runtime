"""Security regressions for the public Skills Hub's copyable commands."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def extractor():
    path = Path(__file__).with_name("extract-skills.py")
    spec = importlib.util.spec_from_file_location("extract_skills", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unified_index_never_publishes_shell_metacharacter_identifier(extractor, tmp_path, monkeypatch):
    index = tmp_path / "skills-index.json"
    index.write_text(json.dumps({"skills": [
        {"name": "safe", "source": "github", "identifier": "openai/skills/safe", "tags": []},
        {"name": "malicious", "source": "github", "identifier": "owner/repo/skill;touch /tmp/pwned"},
        {"name": "substitution", "source": "github", "identifier": "owner/repo/$(id)"},
        {"name": "cmd expansion", "source": "github", "identifier": "owner/repo/%PATH%"},
        {"name": "path traversal", "source": "github", "identifier": "owner/repo/../secret"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(extractor, "UNIFIED_INDEX_PATH", str(index))

    skills, _ = extractor.extract_unified_index_skills()

    assert [skill["identifier"] for skill in skills] == ["openai/skills/safe"]
    assert skills[0]["installCmd"] == "youtab skills install openai/skills/safe"


@pytest.mark.parametrize("entry", [
    {"name": "missing identifier", "source": "github"},
    {"name": "wrong type", "source": "github", "identifier": ["safe"]},
    {"name": "wrong tags", "source": "github", "identifier": "safe", "tags": "security"},
    {"name": "wrong repo", "source": "skills.sh", "identifier": "safe", "repo": ["owner"]},
])
def test_invalid_unified_rows_are_skipped(extractor, entry):
    assert not extractor._valid_unified_entry(entry)


def test_normal_source_identifiers_keep_install_commands(extractor):
    assert extractor._install_command("clawhub", "example-skill") == \
        "youtab skills install clawhub/example-skill"
    assert extractor._install_command("skills.sh", "skills-sh/owner/repo/skill") == \
        "youtab skills install skills-sh/owner/repo/skill"


def test_invalid_source_url_does_not_become_clickable(extractor):
    assert extractor._source_url("unknown", "safe", {"detail_url": "httpjavascript:alert(1)"}) == ""
    assert extractor._source_url("unknown", "safe", {"detail_url": "http://example.com/skill"}) == ""
    assert extractor._source_url("unknown", "safe", {"detail_url": "https://[broken"}) == ""
    assert extractor._source_url("unknown", "safe", {"detail_url": "https://example.com/skill"}) == \
        "https://example.com/skill"
