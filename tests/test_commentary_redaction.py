"""Issue #70: redact sensitive spans in activity commentary, keep safe prose."""

import asyncio

import pytest
from test_activity_card import activity_bridge, message_record

from openrig_matrix_bridge.bridge import sanitize_activity_summary

PATH = "[lokaler Pfad]"
TOKEN = "[Token entfernt]"
CREDENTIALS = "[Zugangsdaten entfernt]"
COOKIE = "[Cookie entfernt]"
OUTPUT = "[Ausgabe entfernt]"


def test_internal_path_is_replaced_and_status_text_kept():
    text = "Daemon gestoppt; Zustand liegt unter /root/.openrig/state/daemon.json."

    assert sanitize_activity_summary(text) == (
        f"Daemon gestoppt; Zustand liegt unter {PATH}."
    )


def test_each_of_several_paths_is_replaced_independently():
    text = "Kopiere /home/agent/a.txt nach /tmp/b.txt und ~/notes/c.md, dann fertig."

    summary = sanitize_activity_summary(text)

    assert summary == f"Kopiere {PATH} nach {PATH} und {PATH}, dann fertig."


@pytest.mark.parametrize(
    ("text", "secret", "marker"),
    [
        ("Push mit ghp_abcdefghij1234567890 erledigt", "ghp_abcdefghij", TOKEN),
        ("Key sk-abcdefghijklmnop1234 gesetzt", "sk-abcdefghijk", TOKEN),
        (
            "curl -H 'Authorization: Bearer abc.def.ghi1234' schlug fehl",
            "abc.def.ghi1234",
            TOKEN,
        ),
        ("Header Cookie: session=abcdef1234567890 gesendet", "abcdef1234567890", COOKIE),
        ("Set-Cookie: sid=abcdef1234567890 erhalten", "abcdef1234567890", COOKIE),
        (
            "Verbunden mit postgres://admin:hunter2pass@db.internal:5432/app",
            "hunter2pass",
            CREDENTIALS,
        ),
        ("API_KEY=super-secret-value geladen", "super-secret-value", TOKEN),
        ("password is correcthorsebattery ok", "correcthorsebattery", TOKEN),
        (
            "Lang a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8 gesehen",
            "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8",
            TOKEN,
        ),
    ],
)
def test_credentials_are_replaced_by_a_category_marker(text, secret, marker):
    summary = sanitize_activity_summary(text)

    assert secret not in summary
    assert marker in summary
    # The prose before the secret survives.
    assert summary.split()[0] == text.split()[0] or summary.startswith(marker)


def test_uri_userinfo_keeps_scheme_and_host_but_not_credentials():
    summary = sanitize_activity_summary(
        "REDIS_URL redis://default:secretpassword@cache.internal:6379 erreichbar"
    )

    assert "secretpassword" not in summary
    assert "default:" not in summary
    assert f"redis://{CREDENTIALS}@cache.internal:6379" in summary
    assert summary.endswith("erreichbar")


def test_fenced_output_is_replaced_as_a_whole():
    summary = sanitize_activity_summary(
        "Tests gelaufen:\n```\nFAILED /root/x.py token=abc\n```\nAlles grün"
    )

    assert summary == f"Tests gelaufen: {OUTPUT} Alles grün"


def test_safe_commentary_is_unchanged():
    text = "Ich prüfe die Tests und starte den Daemon danach neu."

    assert sanitize_activity_summary(text) == text


def test_markers_are_not_redacted_again():
    summary = sanitize_activity_summary("token ghp_abcdefghij1234567890 und /tmp/x")

    assert summary.count("[") == summary.count("]") == 2
    assert "[[" not in summary


def test_whitespace_is_normalized_and_length_bounded():
    summary = sanitize_activity_summary(
        "Schritt   eins\n\nunter /root/a.json\t" + "lang " * 300
    )

    assert "  " not in summary and "\n" not in summary
    assert summary.startswith(f"Schritt eins unter {PATH} lang")
    assert len(summary) <= 600
    assert summary.endswith("…")


def test_truncation_never_leaves_part_of_a_marker():
    summary = sanitize_activity_summary("x" * 590 + " /root/state/daemon.json")

    assert len(summary) <= 600
    assert summary.endswith("…")
    assert "[lok" not in summary or PATH in summary


def test_advisor_style_commentary_keeps_the_card_informative(tmp_path):
    advisor = (
        "Upgrade auf 0.12.0: Daemon gestoppt; Zustand liegt unter "
        "/root/.openrig/state/daemon.json. Backup nach /root/.openrig/backup/2026-10-25 "
        "geschrieben, Neustart folgt."
    )
    bridge, agent, _request, sent, _edited = activity_bridge(
        tmp_path,
        [message_record("comment-1", "commentary", advisor)],
    )

    asyncio.run(bridge._poll_agent(agent))

    card = sent[0][0]
    assert card == (
        "**Aktivität:** Arbeitet\n\n"
        f"Upgrade auf 0.12.0: Daemon gestoppt; Zustand liegt unter {PATH}. "
        f"Backup nach {PATH} geschrieben, Neustart folgt."
    )
    assert "/root/" not in card
    assert ".openrig" not in card
