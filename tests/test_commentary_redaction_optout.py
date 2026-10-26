"""Issue #93: commentary redaction can be turned off per bridge instance."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_activity_card import activity_bridge, message_record
from test_bridge import queued_request
from test_config import write_config

from openrig_matrix_bridge.bridge import (
    MatrixBridge,
    queue_notice_text,
    sanitize_activity_summary,
)
from openrig_matrix_bridge.config import ConfigError, load_config
from openrig_matrix_bridge.formatting import markdown_to_matrix_html

MIXED = (
    "Prüfe /root/.openrig/state/daemon.json mit Cookie: sid=SECRETSID1234 und "
    "Token ghp_abcdefghij1234567890 über postgres://admin:pw@db:5432/app"
)


def test_redaction_stays_on_by_default():
    summary = sanitize_activity_summary(MIXED)

    assert "/root/.openrig" not in summary
    assert "SECRETSID1234" not in summary
    assert "ghp_abcdefghij" not in summary


def test_opt_out_keeps_every_span():
    summary = sanitize_activity_summary(MIXED, redact=False)

    assert summary == MIXED


def test_opt_out_keeps_fenced_output_but_normalizes_and_bounds():
    summary = sanitize_activity_summary(
        "Lauf:\n```\nFAILED /root/x.py\n```\n" + "lang " * 300,
        redact=False,
    )

    assert summary.startswith("Lauf: ``` FAILED /root/x.py ``` lang")
    assert "\n" not in summary and "  " not in summary
    assert len(summary) <= 600
    assert summary.endswith("…")


def test_queue_preview_follows_the_opt_out():
    pending = [
        queued_request("$one", "Active"),
        queued_request("$two", "Lies /home/alice/plan.md", started_at=None),
    ]

    assert "[lokaler Pfad]" in queue_notice_text(pending)
    assert '"Lies /home/alice/plan.md"' in queue_notice_text(pending, redact=False)


def test_unredacted_commentary_is_still_escaped_for_matrix():
    summary = sanitize_activity_summary("<script>alert(1)</script> /root/x", redact=False)
    html = markdown_to_matrix_html(f"**Aktivität:** Arbeitet\n\n{summary}")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "/root/x" in html


def test_opt_out_reaches_the_activity_card(tmp_path: Path):
    bridge, agent, _request, sent, _edited = activity_bridge(
        tmp_path,
        [message_record("comment-1", "commentary", MIXED)],
    )
    bridge.config.commentary_redaction_enabled = False

    asyncio.run(bridge._poll_agent(agent))

    assert sent[0][0] == f"**Aktivität:** Arbeitet\n\n{MIXED}"


def enable(tmp_path: Path, value: str | None) -> Path:
    path = write_config(tmp_path / "config.yaml")
    if value is not None:
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '  human_destinations: ["owner@external"]',
                '  human_destinations: ["owner@external"]\n'
                f"  commentary_redaction_enabled: {value}",
            ),
            encoding="utf-8",
        )
    return path


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("true", True), ("false", False)],
)
def test_config_reads_the_switch(tmp_path: Path, value, expected):
    assert load_config(enable(tmp_path, value)).commentary_redaction_enabled is expected


@pytest.mark.parametrize("value", ['"false"', "maybe", "0", "null"])
def test_config_rejects_a_non_boolean_switch(tmp_path: Path, value):
    with pytest.raises(ConfigError, match="commentary_redaction_enabled"):
        load_config(enable(tmp_path, value))


@pytest.mark.parametrize(("enabled", "warnings"), [(True, 0), (False, 1)])
def test_startup_warns_once_only_when_redaction_is_off(caplog, enabled, warnings):
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(commentary_redaction_enabled=enabled)

    with caplog.at_level(logging.WARNING):
        bridge._warn_if_commentary_redaction_disabled()

    records = [
        record
        for record in caplog.records
        if "commentary_redaction_enabled" in record.getMessage()
    ]
    assert len(records) == warnings
