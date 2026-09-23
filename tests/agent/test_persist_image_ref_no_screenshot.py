"""Adversarial coverage for the attached-image persistence boundary.

Regression: a native-image user turn was persisted as
``caption\\n@image:`...`\\n[screenshot]``. The stray ``[screenshot]`` both
duplicated the image in the durable transcript and hid the thumbnail after a
restart (image-attachment-resume). The fix (`_persist_list_content_to_text`)
suppresses the placeholder ONLY when durable ``@image:`` refs already cover
every image part, so genuine ref-less screenshots keep their placeholder.
"""
from pathlib import Path

import pytest

from agent.tool_dispatch_helpers import _persist_list_content_to_text


def _img(url: str = "data:image/png;base64,AAAA") -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


class TestPersistListContentToText:
    def test_caption_with_one_covered_image_drops_placeholder(self):
        out = _persist_list_content_to_text(
            [{"type": "text", "text": "cap\n@image:`/p/a.png`"}, _img()]
        )
        assert out == "cap\n@image:`/p/a.png`"
        assert "[screenshot]" not in out

    def test_multiple_images_all_covered_drops_all_placeholders(self):
        out = _persist_list_content_to_text(
            [
                {"type": "text", "text": "cap\n@image:`/a.png`\n@image:`/b.png`"},
                _img(),
                _img(),
            ]
        )
        assert out == "cap\n@image:`/a.png`\n@image:`/b.png`"
        assert "[screenshot]" not in out

    def test_legitimate_screenshot_without_ref_keeps_placeholder(self):
        # A tool-produced capture with NO @image: ref must still fall back to
        # the placeholder — the fix is targeted, not a blanket removal.
        out = _persist_list_content_to_text(
            [{"type": "text", "text": "look at this"}, _img()]
        )
        assert out == "look at this\n[screenshot]"

    def test_partial_refs_do_not_cover_all_images_keep_placeholders(self):
        # 1 ref, 2 images -> refs do not cover every image -> honest fallback.
        out = _persist_list_content_to_text(
            [{"type": "text", "text": "cap\n@image:`/a.png`"}, _img(), _img()]
        )
        assert out.count("[screenshot]") == 2
        assert "@image:`/a.png`" in out

    def test_missing_revoked_reference_leaves_no_ref_keeps_placeholder(self):
        # When the ref could not be built (file missing/revoked upstream, so the
        # text carries no @image:), the image still persists as [screenshot].
        out = _persist_list_content_to_text(
            [{"type": "text", "text": "cap"}, _img()]
        )
        assert out == "cap\n[screenshot]"

    def test_text_only(self):
        assert _persist_list_content_to_text([{"type": "text", "text": "hi"}]) == "hi"

    def test_image_only_no_text(self):
        assert _persist_list_content_to_text([_img()]) == "[screenshot]"

    def test_empty_list(self):
        assert _persist_list_content_to_text([]) is None

    def test_input_image_and_image_types_recognized(self):
        out = _persist_list_content_to_text(
            [
                {"type": "text", "text": "c\n@image:`/a`\n@image:`/b`"},
                {"type": "image", "source": {}},
                {"type": "input_image", "image_url": "x"},
            ]
        )
        assert "[screenshot]" not in out


class TestDurableRoundTripAndRestart:
    """Persist through the real SessionDB and re-read after reopening it."""

    def _open(self, tmp_path: Path):
        import youtab_state

        return youtab_state.SessionDB(tmp_path / "state.db")

    def test_image_ref_survives_restart_without_screenshot(self, tmp_path):
        db = self._open(tmp_path)
        sid = db.create_session("s-img", source="desktop")
        clean = _persist_list_content_to_text(
            [{"type": "text", "text": "cap\n@image:`/p/a.png`"}, _img()]
        )
        db.append_message(session_id=sid, role="user", content=clean)

        # Reopen (simulates app/backend restart) and read the durable row.
        db2 = self._open(tmp_path)
        rows = db2.get_messages(sid)
        user = next(r for r in rows if r["role"] == "user")
        assert "[screenshot]" not in (user["content"] or "")
        assert "@image:`/p/a.png`" in user["content"]
        assert user["content"].splitlines()[0] == "cap"

    def test_legitimate_screenshot_row_preserved_across_restart(self, tmp_path):
        db = self._open(tmp_path)
        sid = db.create_session("s-shot", source="desktop")
        shot = _persist_list_content_to_text(
            [{"type": "text", "text": "no ref here"}, _img()]
        )
        db.append_message(session_id=sid, role="user", content=shot)

        db2 = self._open(tmp_path)
        rows = db2.get_messages(sid)
        user = next(r for r in rows if r["role"] == "user")
        assert user["content"] == "no ref here\n[screenshot]"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
