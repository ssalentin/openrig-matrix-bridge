"""Issue #86: the queue card is pinned only while a request waits."""

import asyncio

from test_bridge import queue_notice_bridge, queued_request

from openrig_matrix_bridge.bridge import queue_notice_text
from openrig_matrix_bridge.config import AgentConfig

AGENT = AgentConfig(
    key="advisor",
    display_name="Advisor",
    session="advisor-lead@kernel",
)


def silent_reaction(event_id="$reaction"):
    return queued_request(
        event_id,
        "Matrix-Reaktion von @owner: 👍",
        source_label="reaction",
        started_at=None,
        suppress_reply=True,
    )


def test_one_active_request_without_waiting_has_no_card():
    pending = [queued_request("$one", "Only active prompt")]
    bridge, sent, edited, pinned, unpinned = queue_notice_bridge(pending)

    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 0))

    assert queue_notice_text(pending) is None
    assert sent == [] and edited == [] and pinned == []
    assert unpinned == []


def test_pinned_card_is_unpinned_when_only_the_active_request_is_left():
    pending = [queued_request("$one", "Only active prompt")]
    bridge, sent, edited, pinned, unpinned = queue_notice_bridge(
        pending,
        notice={"event_id": "$queue", "pinned": True},
    )

    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 0))

    assert sent == [] and edited == [] and pinned == []
    assert unpinned == [["$queue"]]
    assert bridge.state.data["queue_notices"]["advisor"]["pinned"] is False


def test_card_pins_for_a_waiting_request_and_unpins_in_place_when_it_starts():
    active = queued_request("$one", "Active prompt")
    pending = [active]
    bridge, sent, edited, pinned, unpinned = queue_notice_bridge(pending)

    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 0))
    pending.append(queued_request("$two", "Waiting prompt", started_at=None))
    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 1))
    pending.pop(0)
    pending[0]["started_at"] = 1.0
    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 0))
    pending.append(queued_request("$three", "Next waiting", started_at=None))
    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 1))

    assert len(sent) == 1
    assert "1 Nachricht wartet" in sent[0]
    assert '1. Text · "Waiting prompt"' in sent[0]
    assert pinned == [["$queue"], ["$queue"]]
    assert unpinned == [["$queue"]]
    assert [event_id for event_id, _text in edited] == ["$queue"]
    assert '1. Text · "Next waiting"' in edited[0][1]


def test_restart_unpins_a_stale_zero_waiting_card():
    bridge, sent, edited, pinned, unpinned = queue_notice_bridge(
        [queued_request("$one", "Active before restart")],
        notice={"event_id": "$queue", "pinned": True},
    )
    bridge.config.agents = {"advisor": AGENT}
    bridge.state.data["rooms"] = {"advisor": {"room_id": "!advisor"}}

    asyncio.run(bridge._restore_queue_notices())

    assert sent == [] and edited == [] and pinned == []
    assert unpinned == [["$queue"]]


def test_silent_feedback_never_counts_as_waiting():
    pending = [queued_request("$one", "Active prompt"), silent_reaction()]
    bridge, sent, edited, pinned, _unpinned = queue_notice_bridge(pending)

    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 1))

    assert queue_notice_text(pending) is None
    assert sent == [] and edited == [] and pinned == []


def test_only_silent_feedback_creates_no_card():
    pending = [silent_reaction("$r1"), silent_reaction("$r2")]
    bridge, sent, edited, pinned, _unpinned = queue_notice_bridge(pending)

    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 1))

    assert sent == [] and edited == [] and pinned == []


def test_steered_follow_up_of_the_active_turn_is_not_waiting():
    active = queued_request("$one", "Active prompt")
    group = queued_request(
        "$group",
        "Steering container",
        source_event_ids=["$s1", "$s2"],
        started_at=None,
    )
    group["id"] = "steering-group"
    group["deferred_steering"] = True
    active["steering_group_id"] = group["id"]

    assert queue_notice_text([active, group]) is None


def test_request_waiting_for_a_busy_seat_keeps_its_card():
    pending = [queued_request("$one", "Waiting for the seat", started_at=None)]
    bridge, sent, _edited, pinned, _unpinned = queue_notice_bridge(pending)

    asyncio.run(bridge._update_queue_notice(AGENT, "!advisor", 0))

    assert "1 Nachricht wartet" in sent[0]
    assert '**Nächste Anfrage:** Text · "Waiting for the seat"' in sent[0]
    assert pinned == [["$queue"]]
