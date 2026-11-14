"""Log redaction: secrets are masked in structured log events (moved from SETH `tests/test_core.py`)."""

from shifde.observability.logging import redact_processor


def test_log_redaction_masks_secrets() -> None:
    out = redact_processor(
        None, "info", {"event": "x", "password": "hunter2", "n": {"token": "t", "ok": 1}}
    )
    assert out["password"] == "[REDACTED]"
    assert out["n"] == {"token": "[REDACTED]", "ok": 1}
