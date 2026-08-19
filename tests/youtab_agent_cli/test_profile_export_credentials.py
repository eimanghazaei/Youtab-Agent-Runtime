"""Tests for credential exclusion during profile export.

Profile exports should NEVER include auth.json or .env — these contain
API keys, OAuth tokens, and credential pool data. Users share exported
profiles; leaking credentials in the archive is a security issue.
"""

import tarfile

from youtab_agent_cli.profiles import export_profile, _DEFAULT_EXPORT_EXCLUDE_ROOT


class TestCredentialExclusion:

    def test_auth_json_in_default_exclude_set(self):
        """auth.json must be in the default export exclusion set."""
        assert "auth.json" in _DEFAULT_EXPORT_EXCLUDE_ROOT


    def test_named_profile_export_excludes_auth(self, tmp_path, monkeypatch):
        """Named profile export must not contain auth.json or .env."""
        profiles_root = tmp_path / "profiles"
        profile_dir = profiles_root / "testprofile"
        profile_dir.mkdir(parents=True)

        # Create a profile with credentials
        (profile_dir / "config.yaml").write_text("model: gpt-4\n", encoding="utf-8")
        (profile_dir / "auth.json").write_text('{"tokens": {"access": "sk-secret"}}', encoding="utf-8")
        (profile_dir / ".env").write_text("OPENROUTER_API_KEY=sk-secret-key\n", encoding="utf-8")
        (profile_dir / "SOUL.md").write_text("I am helpful.\n", encoding="utf-8")
        (profile_dir / "memories").mkdir()
        (profile_dir / "memories" / "MEMORY.md").write_text("# Memories\n", encoding="utf-8")

        monkeypatch.setattr("youtab_agent_cli.profiles._get_profiles_root", lambda: profiles_root)
        monkeypatch.setattr("youtab_agent_cli.profiles.get_profile_dir", lambda n: profile_dir)
        monkeypatch.setattr("youtab_agent_cli.profiles.validate_profile_name", lambda n: None)

        output = tmp_path / "export.tar.gz"
        result = export_profile("testprofile", str(output))

        # Check archive contents
        with tarfile.open(result, "r:gz") as tf:
            names = tf.getnames()

        assert any("config.yaml" in n for n in names), "config.yaml should be in export"
        assert any("SOUL.md" in n for n in names), "SOUL.md should be in export"
        assert not any("auth.json" in n for n in names), "auth.json must NOT be in export"
        assert not any(".env" in n for n in names), ".env must NOT be in export"
