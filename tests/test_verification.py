from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from nio import KeyVerificationStart

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.verification import (
    begin_verification,
    expire_active_verifications,
    mark_verification,
    pending_verification,
    prune_terminal_verifications,
    recover_interrupted_verifications,
)


def test_verification_state_persists_room_and_phase():
    records = {}

    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    mark_verification(records, "tx-one", "awaiting_confirmation", now=110)

    record = records["tx-one"]
    assert record["room_id"] == "!advisor:example.org"
    assert record["phase"] == "awaiting_confirmation"
    assert record["started_at"] == 100
    assert record["updated_at"] == 110
    assert pending_verification(records, "!advisor:example.org", now=111) == (
        "tx-one",
        record,
    )


def test_terminal_verification_is_not_pending():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    mark_verification(records, "tx-one", "verified", now=110)

    assert pending_verification(records, "!advisor:example.org", now=111) is not None
    assert pending_verification(records, "!advisor:example.org", now=1000) is None


def test_recovery_marks_active_transaction_interrupted_once():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)

    interrupted = recover_interrupted_verifications(records, now=200)

    assert interrupted == [("tx-one", "!advisor:example.org")]
    assert records["tx-one"]["phase"] == "interrupted"
    assert records["tx-one"]["terminal_notification"]["state"] == "pending"
    assert pending_verification(records, "!advisor:example.org", now=201) is not None
    assert recover_interrupted_verifications(records, now=202) == []


def test_interrupted_transaction_remains_guarded_after_reply_grace_period():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    recover_interrupted_verifications(records, now=200)

    assert pending_verification(records, "!advisor:example.org", now=500) is not None
    assert pending_verification(records, "!advisor:example.org", now=1000) is not None


@pytest.mark.parametrize(
    "phase",
    ["verified", "rejected", "cancelled", "failed", "timed_out"],
)
def test_recent_terminal_transaction_guards_late_confirmation(phase: str):
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    mark_verification(records, "tx-one", phase, now=200)

    assert pending_verification(records, "!advisor:example.org", now=201) is not None
    assert pending_verification(records, "!advisor:example.org", now=1000) is None


def test_new_verification_supersedes_interrupted_guard():
    records = {}
    begin_verification(records, "tx-old", "!advisor:example.org", now=100)
    recover_interrupted_verifications(records, now=200)

    begin_verification(records, "tx-new", "!advisor:example.org", now=300)
    mark_verification(records, "tx-new", "verified", now=400)

    assert records["tx-old"]["phase"] == "superseded"
    pending = pending_verification(records, "!advisor:example.org", now=500)
    assert pending is not None
    assert pending[0] == "tx-new"


def test_prunes_only_expired_terminal_records():
    records = {}
    begin_verification(records, "tx-terminal", "!advisor:example.org", now=100)
    mark_verification(records, "tx-terminal", "verified", now=200)
    begin_verification(records, "tx-active", "!advisor:example.org", now=100)

    pruned = prune_terminal_verifications(records, now=1000, retention=500)

    assert pruned == ["tx-terminal"]
    assert "tx-terminal" not in records
    assert records["tx-active"]["phase"] == "starting"


def test_expires_each_active_transaction_once():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)

    assert expire_active_verifications(records, now=200, timeout=300) == []
    assert expire_active_verifications(records, now=500, timeout=300) == [
        ("tx-one", "!advisor:example.org")
    ]
    assert records["tx-one"]["phase"] == "timed_out"
    assert records["tx-one"]["terminal_notification"]["state"] == "pending"
    assert expire_active_verifications(records, now=1000, timeout=300) == []


def verification_bridge(records, client=None):
    bridge = object.__new__(MatrixBridge)
    bridge.client = client or SimpleNamespace()
    bridge.client.key_verifications = getattr(
        bridge.client,
        "key_verifications",
        {},
    )
    bridge.verification_records = records
    bridge.processed_events = set()
    bridge.sent = []
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={"processed_events": []},
    )

    async def send(room_id, text, reply_to=None, **kwargs):
        bridge.sent.append((room_id, text, reply_to))
        return SimpleNamespace(event_id="$status")

    bridge._send = send
    return bridge


def test_restart_recovery_marks_and_reports_interruption_once():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    bridge = verification_bridge(records)

    asyncio.run(bridge._recover_verifications())
    asyncio.run(bridge._recover_verifications())

    assert records["tx-one"]["phase"] == "interrupted"
    assert len(bridge.sent) == 1
    assert "Neustart" in bridge.sent[0][1]


def test_restart_delivers_interrupted_notification_saved_before_crash():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    recover_interrupted_verifications(records, now=200)
    bridge = verification_bridge(records)

    asyncio.run(bridge._recover_verifications())
    asyncio.run(bridge._recover_verifications())

    assert len(bridge.sent) == 1
    assert "Neustart" in bridge.sent[0][1]
    notification = records["tx-one"]["terminal_notification"]
    assert notification["state"] == "delivered"
    assert notification["event_id"] == "$status"


def test_restart_delivers_timed_out_notification_saved_before_crash():
    records = {}
    now = time.time()
    begin_verification(records, "tx-one", "!advisor:example.org", now=now - 1000)
    expire_active_verifications(records, now=now - 500, timeout=300)
    bridge = verification_bridge(records)

    asyncio.run(bridge._recover_verifications())
    asyncio.run(bridge._recover_verifications())

    assert len(bridge.sent) == 1
    assert "Zeitüberschreitung" in bridge.sent[0][1]
    notification = records["tx-one"]["terminal_notification"]
    assert notification["state"] == "delivered"
    assert notification["event_id"] == "$status"


def test_restart_reconciles_timed_out_record_without_notification_intent():
    records = {}
    now = time.time()
    begin_verification(records, "tx-one", "!advisor:example.org", now=now - 1000)
    mark_verification(records, "tx-one", "timed_out", now=now - 500)
    bridge = verification_bridge(records)

    asyncio.run(bridge._recover_verifications())

    assert len(bridge.sent) == 1
    assert "Zeitüberschreitung" in bridge.sent[0][1]
    assert records["tx-one"]["terminal_notification"]["state"] == "delivered"


def test_failed_notification_retry_reuses_stable_transaction_id():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    recover_interrupted_verifications(records, now=200)
    bridge = verification_bridge(records)
    attempts = []

    async def fail_then_deliver(room_id, text, reply_to=None, **kwargs):
        attempts.append(kwargs["tx_id"])
        if len(attempts) == 1:
            raise OSError("simulated notification crash")
        return SimpleNamespace(event_id="$delivered")

    bridge._send = fail_then_deliver

    asyncio.run(bridge._recover_verifications())
    assert records["tx-one"]["terminal_notification"]["state"] == "pending"

    asyncio.run(bridge._recover_verifications())

    assert attempts == [
        "verification-tx-one-interrupted",
        "verification-tx-one-interrupted",
    ]
    assert records["tx-one"]["terminal_notification"]["state"] == "delivered"
    assert records["tx-one"]["terminal_notification"]["event_id"] == "$delivered"


def test_send_forwards_verification_transaction_id_to_matrix_client():
    calls = []

    async def room_send(room_id, event_type, content, **kwargs):
        calls.append((room_id, event_type, content, kwargs))
        return SimpleNamespace(event_id="$verified")

    bridge = object.__new__(MatrixBridge)
    bridge.client = SimpleNamespace(room_send=room_send)

    response = asyncio.run(
        bridge._send(
            "!advisor:example.org",
            "verification update",
            tx_id="verification-tx-one-interrupted",
        )
    )

    assert response.event_id == "$verified"
    assert calls[0][3]["tx_id"] == "verification-tx-one-interrupted"


def test_confirmation_is_persisted_before_remote_accept():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    mark_verification(records, "tx-one", "awaiting_confirmation", now=110)

    class Client:
        async def confirm_short_auth_string(self, transaction_id):
            assert records[transaction_id]["phase"] == "confirming"
            return SimpleNamespace()

    bridge = verification_bridge(records, Client())
    room = SimpleNamespace(room_id="!advisor:example.org")
    event = SimpleNamespace(event_id="$reply")

    handled = asyncio.run(
        bridge._handle_verification_reply(room, event, "ja")
    )

    assert handled is True
    assert records["tx-one"]["phase"] == "awaiting_mac"
    assert bridge.sent


def test_interrupted_confirmation_is_not_forwarded_to_agent():
    records = {}
    now = time.time()
    begin_verification(records, "tx-one", "!advisor:example.org", now=now - 10)
    recover_interrupted_verifications(records, now=now)
    forwarded = []

    class Client:
        async def confirm_short_auth_string(self, transaction_id):
            raise AssertionError("interrupted verification must not be confirmed")

    bridge = verification_bridge(records, Client())

    async def enqueue(*args, **kwargs):
        forwarded.append((args, kwargs))

    bridge._enqueue_prompt = enqueue
    bridge.room_agents = {"!advisor:example.org": "advisor"}
    bridge.config = SimpleNamespace(
        agents={
            "advisor": SimpleNamespace(
                key="advisor",
                session="advisor@example.org",
            )
        },
        user_id="@bridge:example.org",
        allowed_users=("@owner:example.org",),
        max_message_age_minutes=-1,
    )
    room = SimpleNamespace(room_id="!advisor:example.org")
    event = SimpleNamespace(
        event_id="$reply",
        sender="@owner:example.org",
        body="ja",
        server_timestamp=0,
    )

    asyncio.run(bridge._handle_room_message(room, event))

    assert forwarded == []
    assert records["tx-one"]["phase"] == "interrupted"
    assert "keine aktive" in bridge.sent[-1][1].lower()

    explicit = SimpleNamespace(
        event_id="$explicit",
        sender="@owner:example.org",
        body="agent: ja",
        server_timestamp=0,
    )
    asyncio.run(bridge._handle_room_message(room, explicit))

    assert len(forwarded) == 1


def test_late_confirmation_after_terminal_state_is_not_forwarded_to_agent():
    records = {}
    now = time.time()
    begin_verification(records, "tx-one", "!advisor:example.org", now=now - 10)
    mark_verification(records, "tx-one", "timed_out", now=now - 1)
    forwarded = []
    bridge = verification_bridge(records)

    async def enqueue(*args, **kwargs):
        forwarded.append((args, kwargs))

    bridge._enqueue_prompt = enqueue
    bridge.room_agents = {"!advisor:example.org": "advisor"}
    bridge.config = SimpleNamespace(
        agents={"advisor": SimpleNamespace(key="advisor", session="advisor@example.org")},
        user_id="@bridge:example.org",
        allowed_users=("@owner:example.org",),
        max_message_age_minutes=-1,
    )
    room = SimpleNamespace(room_id="!advisor:example.org")
    event = SimpleNamespace(
        event_id="$reply",
        sender="@owner:example.org",
        body="ja",
        server_timestamp=0,
    )

    asyncio.run(bridge._handle_room_message(room, event))

    assert forwarded == []
    assert records["tx-one"]["phase"] == "stale_reply"
    assert "keine aktive" in bridge.sent[-1][1].lower()


def test_timeout_marks_and_reports_transaction_once():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    client = SimpleNamespace(
        cancel_key_verification=lambda transaction_id, reject=False: _completed(
            SimpleNamespace()
        )
    )
    bridge = verification_bridge(records, client)

    asyncio.run(bridge._expire_verifications(now=1000, timeout=300))
    asyncio.run(bridge._expire_verifications(now=2000, timeout=300))

    assert records["tx-one"]["phase"] == "timed_out"
    assert len(bridge.sent) == 1
    assert "Zeitüberschreitung" in bridge.sent[0][1]


def test_verification_start_is_persisted_before_client_accept():
    records = {}

    class Client:
        async def accept_key_verification(self, transaction_id):
            assert records[transaction_id]["phase"] == "starting"
            return SimpleNamespace()

        async def to_device(self, message):
            return SimpleNamespace()

    client = Client()
    client.key_verifications = {
        "tx-one": SimpleNamespace(share_key=lambda: SimpleNamespace())
    }
    bridge = verification_bridge(records, client)
    bridge._verification_room = lambda: "!advisor:example.org"
    bridge.config = SimpleNamespace(allowed_users=("@owner:example.org",))
    event = KeyVerificationStart(
        {},
        "@owner:example.org",
        "tx-one",
        "OWNER",
        "m.sas.v1",
        ["curve25519-hkdf-sha256"],
        ["sha256"],
        ["hkdf-hmac-sha256.v2"],
        ["emoji"],
    )

    asyncio.run(bridge._on_verification(event))

    assert records["tx-one"]["phase"] == "awaiting_key"


def test_rejection_is_persisted_before_cancel():
    records = {}
    begin_verification(records, "tx-one", "!advisor:example.org", now=100)
    mark_verification(records, "tx-one", "awaiting_confirmation", now=110)

    class Client:
        async def cancel_key_verification(self, transaction_id, reject=False):
            assert records[transaction_id]["phase"] == "cancelling"
            assert reject is True
            return SimpleNamespace()

    bridge = verification_bridge(records, Client())
    room = SimpleNamespace(room_id="!advisor:example.org")
    event = SimpleNamespace(event_id="$reply")

    assert asyncio.run(bridge._handle_verification_reply(room, event, "nein"))
    assert records["tx-one"]["phase"] == "rejected"


async def _completed(value):
    return value
