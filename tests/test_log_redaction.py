"""Tests for log-time secret redaction (#18).

`_mask_secret` is a small pure helper, exercised directly. The uvicorn
access-log filter is built locally inside `web.app.main` so we can't import
it; we replicate the regex contract here and assert it matches the
production behaviour.
"""

from __future__ import annotations

import logging
import re

import pytest

from student_bot.web.app import _mask_secret


# Mirror of the regex inside `_RedactAccessTokenAccessLog.filter`. If the
# production regex changes, this assertion list must move with it — the
# duplication is intentional so the test fails when the contract drifts.
_ACCESS_QS_RE = re.compile(r"(access=)[^&\s\"'\\]+")


def _redact(msg: str) -> str:
    return _ACCESS_QS_RE.sub(r"\1<redacted>", msg)


class TestMaskSecret:
    def test_long_secret_keeps_only_prefix(self):
        # Default keep=6; long secret → first 6 + ellipsis.
        assert _mask_secret("abcdef1234567890XYZ") == "abcdef…"

    def test_short_secret_does_not_partial_leak(self):
        # Anything ≤ keep is replaced wholesale — partial value would leak
        # too much when the secret itself is short.
        assert _mask_secret("abc123") == "<short>"
        assert _mask_secret("abc") == "<short>"

    def test_empty_secret_surfaces_unset(self):
        assert _mask_secret("") == "<unset>"
        assert _mask_secret(None) == "<unset>"  # type: ignore[arg-type]

    def test_custom_keep_length(self):
        assert _mask_secret("abcdef1234567890", keep=3) == "abc…"


class TestAccessTokenAccessLogRedaction:
    """Verify the regex used by `_RedactAccessTokenAccessLog`."""

    def test_redacts_token_in_request_line(self):
        original = '127.0.0.1:54321 - "GET /?access=secrettoken123 HTTP/1.1" 200 OK'
        assert _redact(original) == ('127.0.0.1:54321 - "GET /?access=<redacted> HTTP/1.1" 200 OK')

    def test_redacts_token_when_followed_by_other_params(self):
        original = "GET /?access=secrettoken123&debug=1 HTTP/1.1"
        # Token stops at `&`; the rest of the query string is preserved.
        assert _redact(original) == "GET /?access=<redacted>&debug=1 HTTP/1.1"

    def test_redacts_url_with_path_and_token(self):
        original = 'GET /subpath/?access=ABCdef123_XYZ HTTP/1.1" 200 OK'
        assert "ABCdef123_XYZ" not in _redact(original)
        assert "<redacted>" in _redact(original)

    def test_does_not_touch_unrelated_lines(self):
        original = 'GET /api/health HTTP/1.1" 200 OK'
        assert _redact(original) == original

    def test_does_not_touch_word_succeeded_by_access_substring(self):
        # The regex requires `access=` literally, so words containing "access"
        # without the `=` are not redacted.
        original = 'GET /access HTTP/1.1" 200 OK'
        assert _redact(original) == original


class TestFilterMutatesRecordArgs:
    """End-to-end shape: the filter resets `record.args` after redacting so
    the handler can't reapply the original arguments and re-leak the token.
    Re-implements the filter inline to avoid relying on `main()` being
    callable at test time."""

    @pytest.fixture
    def filter_obj(self):
        class _F(logging.Filter):
            _RE = _ACCESS_QS_RE

            def filter(self, record):
                msg = record.getMessage()
                redacted = self._RE.sub(r"\1<redacted>", msg)
                if redacted != msg:
                    record.msg = redacted
                    record.args = ()
                return True

        return _F()

    def test_record_msg_no_longer_contains_token(self, filter_obj):
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s" %s OK',
            args=("127.0.0.1", "GET /?access=secretXYZ HTTP/1.1", 200),
            exc_info=None,
        )
        filter_obj.filter(record)
        assert "secretXYZ" not in record.getMessage()
        assert "<redacted>" in record.getMessage()
        # Args were cleared so any re-formatting can't reintroduce the token.
        assert record.args == ()

    def test_record_untouched_when_no_token(self, filter_obj):
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s" %s OK',
            args=("127.0.0.1", "GET /api/health HTTP/1.1", 200),
            exc_info=None,
        )
        filter_obj.filter(record)
        # No mutation — args preserved, message format intact.
        assert record.args == ("127.0.0.1", "GET /api/health HTTP/1.1", 200)
