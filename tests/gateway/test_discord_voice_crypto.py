"""WAVE-29 §4: Discord Voice crypto / RTP packet-path regression tests.

These exercise the REAL ``VoiceReceiver._on_packet`` decryption path against the
REAL PyNaCl AEAD primitive (``nacl.secret.Aead``, XChaCha20-Poly1305), building
the exact RTP wire format Discord sends in ``aead_xchacha20_poly1305_rtpsize``
mode. Only the native Opus decoder — which needs libopus and real Opus frames,
neither of which is a crypto concern — is replaced by an identity stub so the
*decrypted* bytes can be observed. Nothing about the crypto, nonce derivation,
AAD binding, RTP header parsing, or padding/extension handling is mocked.

Why this file exists: PyNaCl was bumped to >=1.6.2 (via a uv override) to close
CVE-2025-69277 while keeping Discord Voice, whose ``discord.py`` still caps
PyNaCl<1.6. This suite proves the AEAD path our receiver depends on works on the
fixed version, and it asserts the running PyNaCl is actually >=1.6.2 so the test
can never pass on the vulnerable release.
"""

from __future__ import annotations

import logging
import struct

import pytest

# Real crypto primitive — the whole point of the test. Skip only if PyNaCl is
# entirely absent from the environment (the required CI job installs it).
nacl_secret = pytest.importorskip("nacl.secret")
import nacl  # noqa: E402
import nacl.exceptions  # noqa: E402

from plugins.platforms.discord.adapter import VoiceReceiver  # noqa: E402


def _pynacl_version_tuple() -> tuple:
    parts = []
    for chunk in str(nacl.__version__).split("."):
        num = "".join(c for c in chunk if c.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)


def test_running_pynacl_is_the_fixed_version():
    """Guard: the crypto path must be validated on the NON-vulnerable PyNaCl.

    CVE-2025-69277 / PYSEC-2026-3002 is fixed in 1.6.2. If the environment ever
    resolves an older PyNaCl (e.g. a pip install that ignored the uv override),
    this fails loudly instead of silently validating a vulnerable build.
    """
    assert _pynacl_version_tuple() >= (1, 6, 2), (
        f"PyNaCl {nacl.__version__} is below the CVE-2025-69277 fix (1.6.2); "
        "the uv override-dependencies pin did not take effect here"
    )


# --------------------------------------------------------------------------- #
# RTP packet construction helpers (mirror Discord's rtpsize AEAD wire format)
# --------------------------------------------------------------------------- #
_PAYLOAD_TYPE = 0x78  # 120 = Discord voice


def _rtp_header(*, seq=1, timestamp=0, ssrc=12345, cc=0, extension=False,
                padding=False) -> bytes:
    byte0 = 0x80  # version 2
    byte0 |= (cc & 0x0F)
    if extension:
        byte0 |= 0x10
    if padding:
        byte0 |= 0x20
    byte1 = _PAYLOAD_TYPE
    return struct.pack(">BBHII", byte0, byte1, seq & 0xFFFF, timestamp & 0xFFFFFFFF,
                       ssrc & 0xFFFFFFFF)


def _encrypt_packet(secret_key: bytes, plaintext: bytes, *, header: bytes,
                    nonce_prefix: bytes = b"\x00\x00\x00\x01") -> bytes:
    """Build one encrypted RTP packet exactly as the receiver expects it.

    Wire layout: ``header || ciphertext(+tag) || 4-byte nonce prefix``. The
    24-byte AEAD nonce is the 4-byte prefix left-aligned, zero-padded; the RTP
    header is the AEAD associated data.
    """
    assert len(nonce_prefix) == 4
    nonce = bytearray(24)
    nonce[:4] = nonce_prefix
    box = nacl_secret.Aead(secret_key)
    enc = box.encrypt(plaintext, bytes(header), bytes(nonce))
    return header + enc.ciphertext + nonce_prefix


class _IdentityDecoder:
    """Stand-in for discord.opus.Decoder: returns decrypted bytes unchanged."""

    def decode(self, data: bytes) -> bytes:
        return bytes(data)


def _make_receiver(secret_key: bytes, *, ssrc=12345):
    r = VoiceReceiver(voice_client=object())
    r._running = True
    r._paused = False
    r._secret_key = secret_key
    r._dave_session = None
    r._bot_ssrc = 0
    # Inject identity decoder so the native Opus decoder is never constructed and
    # the observed buffer equals the decrypted transport payload.
    r._decoders[ssrc] = _IdentityDecoder()
    return r


_KEY = bytes(range(32))  # deterministic 32-byte AEAD key


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
class TestVoiceDecryptRoundTrip:
    def test_valid_packet_decrypts_into_buffer(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc)
        plaintext = b"opus-voice-frame-\x00\x01\x02\xff"
        r._on_packet(_encrypt_packet(_KEY, plaintext, header=header))
        assert bytes(r._buffers[ssrc]) == plaintext

    def test_empty_payload_roundtrips(self):
        ssrc = 999
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc)
        r._on_packet(_encrypt_packet(_KEY, b"", header=header))
        assert bytes(r._buffers[ssrc]) == b""

    def test_large_payload_roundtrips(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc)
        plaintext = bytes([i % 256 for i in range(4000)])
        r._on_packet(_encrypt_packet(_KEY, plaintext, header=header))
        assert bytes(r._buffers[ssrc]) == plaintext

    def test_binary_and_unicode_payloads_roundtrip(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc)
        for plaintext in (b"\x00\x01\x02\x03\xfe\xff",
                          "你好🎙️voice".encode("utf-8"),
                          bytes(range(256))):
            r._buffers[ssrc].clear()
            r._on_packet(_encrypt_packet(_KEY, plaintext, header=header))
            assert bytes(r._buffers[ssrc]) == plaintext

    def test_distinct_nonces_decrypt_independently(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc)
        r._on_packet(_encrypt_packet(_KEY, b"frame-A", header=header,
                                     nonce_prefix=b"\x00\x00\x00\x01"))
        r._on_packet(_encrypt_packet(_KEY, b"frame-B", header=header,
                                     nonce_prefix=b"\x00\x00\x00\x02"))
        assert bytes(r._buffers[ssrc]) == b"frame-A" + b"frame-B"


# --------------------------------------------------------------------------- #
# Authenticated-decryption failure / malformed input
# --------------------------------------------------------------------------- #
class TestVoiceDecryptFailure:
    def test_tampered_ciphertext_is_dropped_not_decoded(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc)
        pkt = bytearray(_encrypt_packet(_KEY, b"secret-frame", header=header))
        pkt[len(header) + 3] ^= 0x01  # flip a ciphertext byte
        r._on_packet(bytes(pkt))
        assert ssrc not in r._buffers or bytes(r._buffers[ssrc]) == b""

    def test_wrong_key_is_dropped(self):
        ssrc = 12345
        r = _make_receiver(bytes([7] * 32), ssrc=ssrc)  # receiver has wrong key
        header = _rtp_header(ssrc=ssrc)
        pkt = _encrypt_packet(_KEY, b"frame", header=header)  # encrypted w/ _KEY
        r._on_packet(pkt)
        assert ssrc not in r._buffers or bytes(r._buffers[ssrc]) == b""

    def test_tampered_aad_header_breaks_authentication(self):
        # The RTP header is the AEAD associated data; mutating it after
        # encryption must fail authentication.
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc, seq=1)
        pkt = bytearray(_encrypt_packet(_KEY, b"frame", header=header))
        pkt[2] ^= 0xFF  # flip a sequence-number byte inside the header (AAD)
        r._on_packet(bytes(pkt))
        assert ssrc not in r._buffers or bytes(r._buffers[ssrc]) == b""

    def test_truncated_packet_is_ignored(self):
        r = _make_receiver(_KEY)
        # shorter than the 16-byte minimum, and header-without-nonce cases
        for pkt in (b"", b"\x80\x78", b"\x80\x78" + b"\x00" * 10,
                    _rtp_header() + b"\x00\x00"):
            r._on_packet(pkt)  # must not raise
        assert not r._buffers

    def test_non_rtp_packet_is_ignored(self):
        r = _make_receiver(_KEY)
        # wrong version bits / wrong payload type
        r._on_packet(b"\x00" * 32)
        r._on_packet(b"\x80" + b"\x7a" + b"\x00" * 30)  # payload type != 0x78
        assert not r._buffers

    def test_invalid_key_length_rejected_by_aead(self):
        with pytest.raises((ValueError, nacl.exceptions.CryptoError,
                            AssertionError, TypeError)):
            nacl_secret.Aead(b"too-short-key")

    def test_bot_own_ssrc_is_skipped(self):
        ssrc = 555
        r = _make_receiver(_KEY, ssrc=ssrc)
        r._bot_ssrc = ssrc  # packet from the bot itself
        header = _rtp_header(ssrc=ssrc)
        r._on_packet(_encrypt_packet(_KEY, b"echo", header=header))
        assert ssrc not in r._buffers or bytes(r._buffers[ssrc]) == b""


# --------------------------------------------------------------------------- #
# RTP padding (RFC 3550 §5.1)
# --------------------------------------------------------------------------- #
class TestVoiceRtpPadding:
    def test_padding_is_stripped(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc, padding=True)
        # payload = 3 real bytes + 2 padding bytes; last byte = pad count (2)
        plaintext = b"abc" + b"\x00\x02"
        r._on_packet(_encrypt_packet(_KEY, plaintext, header=header))
        assert bytes(r._buffers[ssrc]) == b"abc"

    def test_invalid_padding_length_is_dropped(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc, padding=True)
        plaintext = b"ab" + b"\xff"  # pad count 255 > payload -> invalid
        r._on_packet(_encrypt_packet(_KEY, plaintext, header=header))
        assert ssrc not in r._buffers or bytes(r._buffers[ssrc]) == b""


# --------------------------------------------------------------------------- #
# Concurrency, lifecycle, leakage
# --------------------------------------------------------------------------- #
class TestVoiceSessionsAndLifecycle:
    def test_concurrent_ssrc_streams_are_isolated(self):
        r = VoiceReceiver(voice_client=object())
        r._running = True
        r._secret_key = _KEY
        r._dave_session = None
        r._bot_ssrc = 0
        for ssrc in (100, 200):
            r._decoders[ssrc] = _IdentityDecoder()
        r._on_packet(_encrypt_packet(_KEY, b"stream-100", header=_rtp_header(ssrc=100)))
        r._on_packet(_encrypt_packet(_KEY, b"stream-200", header=_rtp_header(ssrc=200)))
        assert bytes(r._buffers[100]) == b"stream-100"
        assert bytes(r._buffers[200]) == b"stream-200"

    def test_paused_receiver_ignores_packets(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        r._paused = True
        r._on_packet(_encrypt_packet(_KEY, b"frame", header=_rtp_header(ssrc=ssrc)))
        assert not r._buffers

    def test_stop_clears_buffers(self):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        r._on_packet(_encrypt_packet(_KEY, b"frame", header=_rtp_header(ssrc=ssrc)))
        assert r._buffers
        r.stop()
        assert not r._buffers

    def test_no_key_or_plaintext_leaks_into_logs(self, caplog):
        ssrc = 12345
        r = _make_receiver(_KEY, ssrc=ssrc)
        header = _rtp_header(ssrc=ssrc)
        # one good packet, one tampered (which logs a warning) to exercise both
        with caplog.at_level(logging.DEBUG):
            r._on_packet(_encrypt_packet(_KEY, b"SENSITIVE-VOICE", header=header))
            bad = bytearray(_encrypt_packet(_KEY, b"SENSITIVE-VOICE", header=header))
            bad[len(header) + 2] ^= 0x01
            r._on_packet(bytes(bad))
        blob = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "SENSITIVE-VOICE" not in blob
        assert _KEY.hex() not in blob
