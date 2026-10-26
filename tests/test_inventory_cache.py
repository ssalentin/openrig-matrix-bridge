"""Issue #96: one cached, single-flight OpenRig node inventory."""

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_config import write_config
from test_openrig import node_payload
from test_steering import append_records, transcript_agent_message

from openrig_matrix_bridge.bridge import MatrixBridge
from openrig_matrix_bridge.config import AgentConfig, ConfigError, load_config
from openrig_matrix_bridge.openrig import AgentSessionNotFound, OpenRigSessionResolver


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Inventory:
    """Counts `rig ps` calls; can block, fail or change between calls."""

    def __init__(self, transcript: Path, *, sessions=("advisor-lead@kernel",)):
        self.transcript = transcript
        self.sessions = sessions
        self.calls = 0
        self.fail = False
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def payload(self):
        entries = []
        for session in self.sessions:
            entry = node_payload(self.transcript)[0]
            entry["canonicalSessionName"] = session
            entries.append(entry)
        return entries

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise subprocess.CalledProcessError(1, args[0], stderr="daemon down")
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(self.payload()), stderr=""
        )


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "rollout.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def resolver(tmp_path, monkeypatch, inventory, *, cache_seconds=2.0, clock=None):
    monkeypatch.setattr(subprocess, "run", inventory)
    return OpenRigSessionResolver(
        "/opt/rig", tmp_path, cache_seconds=cache_seconds, clock=clock or Clock()
    )


def test_resolves_within_the_freshness_bound_from_one_inventory(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    clock = Clock()
    rig = resolver(tmp_path, monkeypatch, inventory, clock=clock)

    rig.resolve("advisor-lead@kernel")
    clock.now += 1.9
    rig.resolve_many({"advisor-lead@kernel"})

    assert inventory.calls == 1


def test_an_expired_snapshot_is_read_again(tmp_path, monkeypatch, transcript):
    inventory = Inventory(transcript)
    clock = Clock()
    rig = resolver(tmp_path, monkeypatch, inventory, clock=clock)

    rig.resolve("advisor-lead@kernel")
    clock.now += 2.1
    rig.resolve("advisor-lead@kernel")

    assert inventory.calls == 2


def test_concurrent_reads_share_one_inventory_call(tmp_path, monkeypatch, transcript):
    inventory = Inventory(transcript)
    inventory.gate = threading.Event()
    rig = resolver(tmp_path, monkeypatch, inventory)
    results = []

    def read():
        results.append(rig.resolve("advisor-lead@kernel").session_id)

    threads = [threading.Thread(target=read) for _ in range(8)]
    threads[0].start()
    inventory.started.wait(5)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.05)
    inventory.gate.set()
    for thread in threads:
        thread.join(5)

    assert inventory.calls == 1
    assert len(results) == 8 and len(set(results)) == 1


def test_a_failed_inventory_reaches_every_waiter_and_is_not_cached(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    inventory.fail = True
    inventory.gate = threading.Event()
    rig = resolver(tmp_path, monkeypatch, inventory)
    errors = []

    def read():
        try:
            rig.resolve("advisor-lead@kernel")
        except AgentSessionNotFound as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=read) for _ in range(3)]
    threads[0].start()
    inventory.started.wait(5)
    for thread in threads[1:]:
        thread.start()
    time.sleep(0.05)
    inventory.gate.set()
    for thread in threads:
        thread.join(5)

    assert inventory.calls == 1
    assert len(errors) == 3 and all("inventory failed" in error for error in errors)
    inventory.fail = False
    inventory.gate = None
    assert rig.resolve("advisor-lead@kernel").session_id
    assert inventory.calls == 2


def test_without_a_freshness_bound_every_read_asks_openrig(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    rig = resolver(tmp_path, monkeypatch, inventory, cache_seconds=0)

    rig.resolve("advisor-lead@kernel")
    rig.resolve("advisor-lead@kernel")

    assert inventory.calls == 2


AGENTS = {
    key: AgentConfig(key=key, display_name=key, session=f"{key}-lead@kernel")
    for key in ("advisor", "ebay", "linkedin")
}


def polling_bridge(tmp_path, monkeypatch, inventory, pending=None):
    bridge = object.__new__(MatrixBridge)
    bridge.config = SimpleNamespace(
        agents=AGENTS,
        mirror_commentary=False,
        response_timeout_minutes=30,
        inventory_cache_seconds=2.0,
    )
    bridge.resolver = resolver(tmp_path, monkeypatch, inventory)
    bridge.locks = {key: asyncio.Lock() for key in AGENTS}
    bridge.state = SimpleNamespace(
        save=lambda: None,
        data={
            "pending": pending or {},
            "queue_notices": {},
            "transcripts": {},
            "rooms": {key: {"room_id": f"!{key}"} for key in AGENTS},
        },
    )

    async def nothing(*args, **kwargs):
        return None

    bridge._recover_acceptance_reactions = nothing
    bridge._sync_typing = nothing
    return bridge


def test_one_poll_cycle_reads_one_inventory_for_every_agent(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(
        transcript, sessions=tuple(agent.session for agent in AGENTS.values())
    )
    bridge = polling_bridge(tmp_path, monkeypatch, inventory)

    asyncio.run(bridge._poll_answers_once())

    assert inventory.calls == 1
    assert set(bridge.state.data["transcripts"]) == set(AGENTS)


def test_starting_a_waiting_request_reads_the_inventory_off_the_event_loop(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    loop_threads = []

    def recording(*args, **kwargs):
        loop_threads.append(threading.current_thread() is threading.main_thread())
        return inventory(*args, **kwargs)

    request = {
        "id": "one",
        "event_id": "$one",
        "started_at": None,
        "prompt_phase": "pending",
        "text": "work",
    }
    bridge = polling_bridge(
        tmp_path, monkeypatch, recording, pending={"advisor": [request]}
    )
    started = []

    async def start_request(agent, req, room_id, ref=None):
        started.append(ref.session_id)

    bridge._start_request = start_request
    bridge._room_for_agent = lambda key: f"!{key}"

    asyncio.run(bridge._poll_agent(AGENTS["advisor"]))

    assert started and loop_threads and not any(loop_threads)


@pytest.mark.parametrize(
    ("value", "expected"), [(None, 2.0), ("0", 0.0), ("0.5", 0.5)]
)
def test_config_reads_the_inventory_freshness_bound(tmp_path, value, expected):
    path = write_config(tmp_path / "config.yaml")
    if value is not None:
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                '  human_destinations: ["owner@external"]',
                '  human_destinations: ["owner@external"]\n'
                f"  inventory_cache_seconds: {value}",
            ),
            encoding="utf-8",
        )

    assert load_config(path).inventory_cache_seconds == expected


@pytest.mark.parametrize("value", ["-1", "11"])
def test_config_rejects_an_unbounded_or_negative_freshness_bound(tmp_path, value):
    path = write_config(tmp_path / "config.yaml")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '  human_destinations: ["owner@external"]',
            '  human_destinations: ["owner@external"]\n'
            f"  inventory_cache_seconds: {value}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="inventory_cache_seconds"):
        load_config(path)


def test_latency_samples_carry_the_recorded_spans():
    bridge = object.__new__(MatrixBridge)
    bridge.state = SimpleNamespace(save=lambda: None, data={})
    request = {
        "id": "one",
        "event_id": "$one",
        "intake_at": 1.0,
        "latency_spans": {"inventory_ms": 12.5, "pane_send_ms": 40.0},
    }

    bridge._record_latency(request, "prompt")

    sample = bridge.state.data["latency_samples"][0]
    assert sample["spans"] == {"inventory_ms": 12.5, "pane_send_ms": 40.0}


def test_a_stale_snapshot_is_served_at_once_while_one_read_refreshes_it(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    clock = Clock()
    monkeypatch.setattr(subprocess, "run", inventory)
    rig = OpenRigSessionResolver(
        "/opt/rig", tmp_path, cache_seconds=2.0, stale_seconds=10.0, clock=clock
    )
    first = rig.resolve("advisor-lead@kernel")
    inventory.gate = threading.Event()
    inventory.started.clear()
    clock.now += 5.0

    started = time.monotonic()
    stale = [rig.resolve("advisor-lead@kernel") for _ in range(3)]
    waited = time.monotonic() - started

    assert waited < 1.0
    assert all(ref == first for ref in stale)
    assert inventory.started.wait(5)
    inventory.gate.set()
    deadline = time.monotonic() + 5
    while rig._in_flight is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert inventory.calls == 2


def test_past_the_hard_bound_a_reader_waits_for_a_fresh_read(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    clock = Clock()
    monkeypatch.setattr(subprocess, "run", inventory)
    rig = OpenRigSessionResolver(
        "/opt/rig", tmp_path, cache_seconds=2.0, stale_seconds=10.0, clock=clock
    )
    rig.resolve("advisor-lead@kernel")
    inventory.fail = True
    clock.now += 11.0

    with pytest.raises(AgentSessionNotFound):
        rig.resolve("advisor-lead@kernel")
    assert inventory.calls == 2


@pytest.mark.parametrize(
    ("cache", "max_age", "error"),
    [(None, None, None), ("2", "1", "inventory_max_age_seconds"), ("2", "61", "inventory_max_age_seconds")],
)
def test_config_bounds_the_inventory_max_age(tmp_path, cache, max_age, error):
    path = write_config(tmp_path / "config.yaml")
    extra = "".join(
        f"\n  {key}: {value}"
        for key, value in (
            ("inventory_cache_seconds", cache),
            ("inventory_max_age_seconds", max_age),
        )
        if value is not None
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '  human_destinations: ["owner@external"]',
            '  human_destinations: ["owner@external"]' + extra,
        ),
        encoding="utf-8",
    )

    if error:
        with pytest.raises(ConfigError, match=error):
            load_config(path)
    else:
        assert load_config(path).inventory_max_age_seconds == 10.0


def stale_idle_harness(tmp_path, observed_offset):
    from test_steering import SteeringHarness

    tmp_path.mkdir(parents=True, exist_ok=True)
    harness = SteeringHarness(tmp_path, "steer")
    harness.enqueue("$root", "root request", None)
    active = harness.bridge.state.data["pending"]["advisor"][0]
    injected_at = float(active["injected_at"])
    original = harness.resolve

    def resolve(session):
        ref = original(session)
        return SimpleNamespace(
            **vars(ref), observed_at=injected_at + observed_offset
        )

    harness.resolve = resolve
    harness.enqueue("$follow", "short follow-up", "$root")
    group = harness.bridge.state.data["pending"]["advisor"][1]
    group["injection_ready_at"] = 0
    harness.busy = False
    return harness, group


def test_an_idle_inventory_older_than_the_active_prompt_does_not_cancel_steering(
    tmp_path,
):
    # The inventory was read before the active prompt went in, so its "idle" is
    # about the previous moment (#96); the transcript shows the turn running.
    harness, group = stale_idle_harness(tmp_path / "stale", -1.0)

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert len(harness.steers) == 1
    assert group["injection_phase"] == "admitted"


def test_an_idle_inventory_read_after_the_active_prompt_still_falls_back(tmp_path):
    harness, group = stale_idle_harness(tmp_path / "fresh", 1.0)

    asyncio.run(harness.bridge._poll_agent(harness.agent))

    assert harness.steers == []
    assert group["injection_phase"] == "fallback"
    assert group["last_injection_error"] == "no active OpenRig turn"


def test_resolved_sessions_carry_the_time_their_inventory_was_read(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    before = time.time()
    ref = resolver(tmp_path, monkeypatch, inventory).resolve("advisor-lead@kernel")

    assert before <= ref.observed_at <= time.time()


def finished_turn_harness(tmp_path, observed_offset):
    from test_steering import SteeringHarness

    tmp_path.mkdir(parents=True, exist_ok=True)
    harness = SteeringHarness(tmp_path, "queue")
    harness.enqueue("$first", "first request", None)
    append_records(
        harness.transcript,
        [transcript_agent_message("turn-1", "final-1", "final_answer", "Done.")],
    )
    asyncio.run(harness.bridge._poll_agent(harness.agent))
    assert harness.sent_answers == [("Done.", "$first")]
    finished_at = time.time()
    original = harness.resolve

    def resolve(session):
        ref = original(session)
        return SimpleNamespace(**vars(ref), observed_at=finished_at + observed_offset)

    harness.resolve = resolve
    harness.busy = True
    harness.enqueue("$second", "second request", None)
    return harness


def test_a_busy_inventory_older_than_the_finished_turn_does_not_hold_a_prompt(
    tmp_path,
):
    # The read happened while the previous turn still ran (#96); the bridge saw
    # that turn finish afterwards, so the next prompt goes in at once.
    harness = finished_turn_harness(tmp_path / "stale", -1.0)

    assert [prompt[1] for prompt in harness.prompts] == [
        "first request",
        "second request",
    ]


def test_a_busy_inventory_read_after_the_finished_turn_still_holds_a_prompt(
    tmp_path,
):
    harness = finished_turn_harness(tmp_path / "fresh", 1.0)

    assert [prompt[1] for prompt in harness.prompts] == ["first request"]


def test_an_unexpected_read_error_does_not_wedge_later_reads(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return inventory(*args, **kwargs)

    rig = resolver(tmp_path, monkeypatch, flaky)

    with pytest.raises(AgentSessionNotFound):
        rig.resolve("advisor-lead@kernel")
    assert rig._in_flight is None
    assert rig.resolve("advisor-lead@kernel").session_id


def test_one_poll_cycle_shares_its_snapshot_even_without_a_cache(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(
        transcript, sessions=tuple(agent.session for agent in AGENTS.values())
    )
    bridge = polling_bridge(tmp_path, monkeypatch, inventory)
    bridge.resolver.cache_seconds = 0.0
    bridge.resolver.stale_seconds = 0.0

    asyncio.run(bridge._poll_answers_once())

    assert inventory.calls == 1


def test_freshness_counts_from_the_start_of_the_read(tmp_path, monkeypatch, transcript):
    clock = Clock()
    inventory = Inventory(transcript)

    def slow(*args, **kwargs):
        clock.now += 1.5  # the read itself takes 1.5 s
        return inventory(*args, **kwargs)

    rig = resolver(tmp_path, monkeypatch, slow, clock=clock)
    rig.resolve("advisor-lead@kernel")
    clock.now += 0.6  # 2.1 s after the read started, 0.6 s after it ended

    rig.resolve("advisor-lead@kernel")

    assert inventory.calls == 2


def test_an_unexpected_error_in_a_background_read_does_not_wedge_later_reads(
    tmp_path, monkeypatch, transcript
):
    inventory = Inventory(transcript)
    clock = Clock()
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return inventory(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", flaky)
    rig = OpenRigSessionResolver(
        "/opt/rig", tmp_path, cache_seconds=2.0, stale_seconds=10.0, clock=clock
    )
    rig.resolve("advisor-lead@kernel")
    clock.now += 5.0

    assert rig.resolve("advisor-lead@kernel").session_id  # stale, refresh fails
    deadline = time.monotonic() + 5
    while rig._in_flight is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rig._in_flight is None
    clock.now += 6.0

    assert rig.resolve("advisor-lead@kernel").session_id
    assert calls["count"] == 3
