"""The completion gate cannot be programmed by the thing it judges.

The uploaded kit's gate read ``verify_by`` out of the task spec and ran it
through ``subprocess.run(cmd, shell=True)``. A task spec is a document a model
writes, so that made the verdict-producing component executable by its own
subject. These tests pin the inversion: a spec names a governed id, the
registry holds the argv, and anything else is refused rather than run.

The rest pin fail-closed. The kit failed open in three places -- a missing
audit script read as "clean", an unsupplied repo read as "clean", and
``files_changed`` was believed because the agent wrote it down. Each of those
has a test here that fails if the behaviour comes back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.youtab.completion_gate import (  # noqa: E402
    GateError,
    evaluate,
    load_registry,
    run_verification,
    sanitized_env,
)

REGISTRY_PATH = REPO / "scripts" / "youtab" / "verifications.toml"


@pytest.fixture(scope="module")
def registry() -> dict:
    return load_registry(REGISTRY_PATH)


def _spec(*verify_by: str) -> dict:
    return {
        "task_id": "t-1",
        "acceptance_criteria": [
            {"id": f"c{i}", "verify_by": v} for i, v in enumerate(verify_by)
        ],
    }


def _evidence(**kw) -> dict:
    base = {"agent_self_assessment": {"claims_success": True}}
    base.update(kw)
    return base


# --- the registry is the only source of commands ----------------------------


class TestNoSpecSuppliedCommands:
    def test_a_shell_string_in_the_spec_is_refused_not_executed(self, registry, tmp_path):
        """The kit's exact shape: a command where an id belongs.

        It must be refused by name. If this ever executes, the gate is
        programmable by its subject again.
        """
        canary = tmp_path / "pwned"
        spec = _spec(f"touch {canary}")

        verdict = evaluate(
            spec, _evidence(), registry=registry, repo=REPO, base="HEAD",
        )

        assert canary.exists() is False, "the gate executed a spec-supplied string"
        assert verdict["verdict"] == "GOAL_NOT_REACHED"
        assert verdict["criteria"][0]["status"] == "unknown_verification"
        assert any("not a governed verification id" in b
                   for b in verdict["blocking_issues"])

    @pytest.mark.parametrize("payload", [
        "runtime-suite; echo owned",
        "runtime-suite && echo owned",
        "runtime-suite | tee /tmp/owned",
        "$(echo runtime-suite)",
        "`echo runtime-suite`",
        "runtime-suite\necho owned",
    ])
    def test_shell_metacharacters_never_reach_a_shell(self, registry, payload):
        """Each of these is a valid id plus shell syntax. None is a valid id."""
        verdict = evaluate(
            _spec(payload), _evidence(), registry=registry, repo=REPO, base="HEAD",
        )
        assert verdict["criteria"][0]["status"] == "unknown_verification"

    def test_a_non_string_verify_by_is_refused(self, registry):
        spec = {"task_id": "t", "acceptance_criteria": [
            {"id": "c0", "verify_by": ["runtime-suite"]},
        ]}
        verdict = evaluate(spec, _evidence(), registry=registry, repo=REPO, base="HEAD")
        assert verdict["criteria"][0]["status"] == "unknown_verification"

    def test_every_registry_entry_is_an_argument_array(self, registry):
        for entry in registry.values():
            assert isinstance(entry["argv"], list)
            assert all(isinstance(a, str) for a in entry["argv"])
            assert entry["argv"], "an empty argv would execute nothing and pass"

    def test_run_verification_passes_a_list_and_never_a_shell(self, registry, monkeypatch):
        """Pin the call itself: `shell=False` and argv as a list."""
        seen = {}

        class _Proc:
            returncode = 0
            stdout = b""
            stderr = b""

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["shell"] = kwargs.get("shell")
            return _Proc()

        monkeypatch.setattr(
            "scripts.youtab.completion_gate.subprocess.run", fake_run
        )
        run_verification(registry["ruff"], REPO)

        assert seen["shell"] is False
        assert isinstance(seen["argv"], list)


# --- fail closed ------------------------------------------------------------


class TestFailsClosed:
    def test_a_missing_registry_is_an_error_not_an_empty_pass(self, tmp_path):
        with pytest.raises(GateError):
            load_registry(tmp_path / "nope.toml")

    def test_an_unreadable_registry_is_an_error(self, tmp_path):
        bad = tmp_path / "v.toml"
        bad.write_text("this is not toml = = =", encoding="utf-8")
        with pytest.raises(GateError):
            load_registry(bad)

    def test_an_empty_registry_is_an_error(self, tmp_path):
        empty = tmp_path / "v.toml"
        empty.write_text("schema_version = 1\n", encoding="utf-8")
        with pytest.raises(GateError):
            load_registry(empty)

    def test_an_unbounded_timeout_is_refused(self, tmp_path):
        bad = tmp_path / "v.toml"
        bad.write_text(
            'schema_version = 1\n[[verification]]\nid = "x"\nargv = ["true"]\n'
            "timeout_s = 999999\n",
            encoding="utf-8",
        )
        with pytest.raises(GateError):
            load_registry(bad)

    def test_a_duplicate_id_is_refused(self, tmp_path):
        """Otherwise the later entry silently shadows a reviewed one."""
        bad = tmp_path / "v.toml"
        bad.write_text(
            'schema_version = 1\n'
            '[[verification]]\nid = "x"\nargv = ["true"]\ntimeout_s = 10\n'
            '[[verification]]\nid = "x"\nargv = ["false"]\ntimeout_s = 10\n',
            encoding="utf-8",
        )
        with pytest.raises(GateError):
            load_registry(bad)

    def test_a_spec_with_no_criteria_cannot_reach_the_goal(self, registry):
        verdict = evaluate(
            {"task_id": "t", "acceptance_criteria": []},
            _evidence(), registry=registry, repo=REPO, base="HEAD",
        )
        assert verdict["verdict"] == "GOAL_NOT_REACHED"
        assert any("nothing is provable" in b for b in verdict["blocking_issues"])

    def test_a_criterion_without_verify_by_blocks(self, registry):
        spec = {"task_id": "t", "acceptance_criteria": [{"id": "c0"}]}
        verdict = evaluate(spec, _evidence(), registry=registry, repo=REPO, base="HEAD")
        assert verdict["verdict"] == "GOAL_NOT_REACHED"

    def test_git_that_cannot_answer_blocks_rather_than_passes(self, registry, tmp_path):
        """A non-repository cwd must not read as "nothing changed, fine"."""
        verdict = evaluate(
            _spec(), _evidence(), registry=registry, repo=tmp_path, base="HEAD",
        )
        assert verdict["verdict"] == "GOAL_NOT_REACHED"
        assert any("git could not report" in b for b in verdict["blocking_issues"])


# --- evidence is scored, not believed ---------------------------------------


class TestEvidenceIsNotAuthority:
    def test_a_self_reported_diff_that_contradicts_git_blocks(self, registry, monkeypatch):
        """The agent says it changed 40 files; git says otherwise."""
        monkeypatch.setattr(
            "scripts.youtab.completion_gate.git_files_changed", lambda repo, base: 2
        )
        verdict = evaluate(
            _spec(), _evidence(diff={"files_changed": 40}),
            registry=registry, repo=REPO, base="HEAD",
        )
        assert any("git reports 2" in b for b in verdict["blocking_issues"])

    def test_a_denied_action_blocks_even_when_success_is_claimed(self, registry, monkeypatch):
        monkeypatch.setattr(
            "scripts.youtab.completion_gate.git_files_changed", lambda repo, base: 5
        )
        verdict = evaluate(
            _spec(), _evidence(denied_actions=["Write(.git/config)"]),
            registry=registry, repo=REPO, base="HEAD",
        )
        assert verdict["verdict"] == "GOAL_NOT_REACHED"
        assert verdict["agent_claimed_success"] is True
        assert verdict["claim_matched_reality"] is False

    def test_the_disagreement_is_recorded(self, registry, monkeypatch):
        """Calibration only exists if the mismatch is written down."""
        monkeypatch.setattr(
            "scripts.youtab.completion_gate.git_files_changed", lambda repo, base: 0
        )
        verdict = evaluate(
            _spec(), _evidence(), registry=registry, repo=REPO, base="HEAD",
        )
        assert verdict["agent_claimed_success"] is True
        assert verdict["verdict"] == "GOAL_NOT_REACHED"
        assert verdict["claim_matched_reality"] is False


# --- environment ------------------------------------------------------------


class TestSanitizedEnvironment:
    def test_a_provider_key_is_not_inherited(self, monkeypatch):
        """A verification subprocess must not receive credentials.

        This is defence in depth, not the boundary: the boundary is that the
        secret is not in this process either. That is the secret broker, and
        it is a later slice.
        """
        monkeypatch.setenv("EXAMPLE_UPSTREAM_API_KEY", "sk-" + "x" * 40)
        monkeypatch.setenv("YOUTAB_AUTHZ_ROSTER", '{"u":{"role":"youtab_owner"}}')
        env = sanitized_env()
        assert "EXAMPLE_UPSTREAM_API_KEY" not in env
        assert "YOUTAB_AUTHZ_ROSTER" not in env

    def test_the_allowlist_is_a_list_not_a_denylist(self, monkeypatch):
        """An unknown variable is dropped, so a new secret needs no new rule."""
        monkeypatch.setenv("SOME_FUTURE_PROVIDER_TOKEN", "value")
        assert "SOME_FUTURE_PROVIDER_TOKEN" not in sanitized_env()

    def test_what_a_subprocess_genuinely_needs_survives(self):
        env = sanitized_env()
        assert "PATH" in env
        assert env["PYTHONHASHSEED"] == "0"


# --- the registry ships something real --------------------------------------


def test_the_shipped_registry_loads_and_declares_no_network():
    """Every verification runs offline unless it says otherwise."""
    registry = load_registry(REGISTRY_PATH)
    assert registry, "the shipped registry must not be empty"
    for entry in registry.values():
        assert entry["network"] is False, (
            f"{entry['id']} declares egress; the sandbox must allowlist it explicitly"
        )


def test_the_registry_is_valid_json_serialisable_for_evidence():
    registry = load_registry(REGISTRY_PATH)
    json.dumps(registry)
