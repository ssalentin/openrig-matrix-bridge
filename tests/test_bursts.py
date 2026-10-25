import pytest

from openrig_matrix_bridge.bursts import (
    MAX_BURSTS,
    assign_part,
    deliverable_parts,
    is_linked,
    prune_bursts,
    relation_reference,
    render_burst,
)

ROOM = "!room:example.org"
OWNER = "@owner:example.org"
T0 = 1_700_000_000_000


def thread(root: str) -> dict:
    return {"rel_type": "m.thread", "event_id": root, "m.in_reply_to": {"event_id": root}}


def add(
    bursts: dict,
    event_id: str,
    kind: str,
    seconds: float,
    *,
    relation: dict | None = None,
    sender: str = OWNER,
    room: str = ROOM,
    inactivity: float = 45,
    max_span: float = 300,
) -> dict:
    burst, _part = assign_part(
        bursts,
        agent_key="ebay",
        room_id=room,
        sender=sender,
        event_id=event_id,
        kind=kind,
        server_ts=T0 + int(seconds * 1000),
        relation=relation,
        thread_root_event_id=event_id,
        reply_to_event_id=event_id,
        inactivity_seconds=inactivity,
        max_span_seconds=max_span,
    )
    return burst


@pytest.mark.parametrize("gap", [1.1, 29.5, 45.0])
def test_text_after_a_gallery_joins_it_within_the_inactivity_boundary(gap):
    bursts: dict = {}
    gallery = add(bursts, "$gallery", "gallery", 0)
    text = add(bursts, "$text", "text", gap)

    assert text is gallery
    assert [part["event_id"] for part in gallery["parts"]] == ["$gallery", "$text"]
    assert gallery["anchor_event_id"] == "$gallery"


def test_text_beyond_the_inactivity_boundary_starts_its_own_burst():
    bursts: dict = {}
    gallery = add(bursts, "$gallery", "gallery", 0)
    text = add(bursts, "$text", "text", 45.5)

    assert text is not gallery
    assert text["anchor_event_id"] == "$text"


def test_two_plain_top_level_texts_inside_the_window_stay_separate():
    bursts: dict = {}
    first = add(bursts, "$one", "text", 0)
    second = add(bursts, "$two", "text", 2)

    assert first is not second
    assert not is_linked(first) and not is_linked(second)


def test_a_text_just_before_a_gallery_joins_it():
    bursts: dict = {}
    text = add(bursts, "$text", "text", 0)
    gallery = add(bursts, "$gallery", "gallery", 5)

    assert gallery is text
    assert is_linked(text)


def test_voice_text_and_file_chain_into_one_burst():
    bursts: dict = {}
    voice = add(bursts, "$voice", "voice", 0)
    add(bursts, "$text", "text", 3)
    add(bursts, "$file", "file", 6)

    assert [part["event_id"] for part in voice["parts"]] == ["$voice", "$text", "$file"]


def test_maximum_span_bounds_a_chain_of_short_gaps():
    bursts: dict = {}
    first = add(bursts, "$g0", "gallery", 0)
    later = [add(bursts, f"$g{index}", "gallery", index * 40) for index in range(1, 10)]

    # Every gap is 40 s, but the burst ends at 300 s: $g7 (280 s) is the last.
    assert [part["event_id"] for part in first["parts"]] == [
        f"$g{index}" for index in range(8)
    ]
    assert later[7] is not first
    assert [part["event_id"] for part in later[7]["parts"]] == ["$g8", "$g9"]


def test_other_sender_or_other_room_never_joins():
    bursts: dict = {}
    gallery = add(bursts, "$gallery", "gallery", 0)

    assert add(bursts, "$other", "text", 1, sender="@friend:example.org") is not gallery
    assert add(bursts, "$room", "text", 1, room="!elsewhere:example.org") is not gallery


def test_zero_inactivity_disables_timing_correlation():
    bursts: dict = {}
    gallery = add(bursts, "$gallery", "gallery", 0, inactivity=0)

    assert add(bursts, "$text", "text", 1, inactivity=0) is not gallery


def test_thread_relation_wins_over_timing():
    bursts: dict = {}
    gallery = add(bursts, "$gallery", "gallery", 0)
    # Long after the inactivity boundary, but inside the gallery's thread.
    reply = add(bursts, "$reply", "text", 200, relation=thread("$gallery"))
    # A top-level text right after it is not part of the thread.
    unrelated = add(bursts, "$top", "text", 201)

    assert reply is gallery
    assert unrelated is not gallery


def test_parts_in_an_unknown_thread_share_a_thread_burst():
    bursts: dict = {}
    first = add(bursts, "$a", "gallery", 0, relation=thread("$root"))
    second = add(bursts, "$b", "text", 120, relation=thread("$root"))
    top_level = add(bursts, "$c", "text", 121)

    assert second is first
    assert first["relation"] == "thread"
    assert first["anchor_event_id"] == "$root"
    assert top_level is not first


def test_unthreaded_part_never_joins_a_thread_burst_by_timing():
    bursts: dict = {}
    threaded = add(bursts, "$a", "gallery", 0, relation=thread("$root"))

    assert add(bursts, "$b", "text", 1) is not threaded


def test_reply_joins_the_burst_of_the_part_it_answers_only():
    bursts: dict = {}
    gallery = add(bursts, "$gallery", "gallery", 0)
    reply = add(
        bursts,
        "$reply",
        "text",
        100,
        relation={"m.in_reply_to": {"event_id": "$gallery"}},
    )
    stranger_reply = add(
        bursts,
        "$other",
        "text",
        101,
        relation={"m.in_reply_to": {"event_id": "$unknown"}},
    )

    assert reply is gallery
    assert stranger_reply is not gallery
    assert stranger_reply["relation"] == "reply"
    # A later unthreaded attachment does not join a reply burst by timing.
    assert add(bursts, "$file", "file", 102) not in (stranger_reply,)


def test_edits_and_other_relations_stay_on_their_own():
    bursts: dict = {}
    gallery = add(bursts, "$gallery", "gallery", 0)
    edit = add(
        bursts,
        "$edit",
        "text",
        1,
        relation={"rel_type": "m.replace", "event_id": "$gallery"},
    )

    assert edit is not gallery
    assert edit["relation"] == "other"


def test_relation_reference_classifies_blocks():
    assert relation_reference(None) == ("none", None)
    assert relation_reference(thread("$root")) == ("thread", "$root")
    assert relation_reference({"m.in_reply_to": {"event_id": "$x"}}) == ("reply", "$x")
    assert relation_reference({"rel_type": "m.replace", "event_id": "$x"}) == (
        "other",
        None,
    )


def test_deliverable_parts_never_overtake_a_part_in_progress():
    bursts: dict = {}
    burst = add(bursts, "$voice", "voice", 0)
    add(bursts, "$text", "text", 1)
    burst["parts"][1].update(phase="ready", text="hello")

    assert deliverable_parts(burst) == []

    burst["parts"][0].update(phase="ready", text="transcript")
    assert [part["event_id"] for part in deliverable_parts(burst)] == ["$voice", "$text"]


def test_render_lists_every_part_in_order_and_marks_earlier_ones():
    bursts: dict = {}
    burst = add(bursts, "$gallery", "gallery", 0)
    add(bursts, "$file", "file", 2)
    add(bursts, "$text", "text", 30)
    burst["parts"][0].update(phase="delivered", text="Galerie von @owner …")
    burst["parts"][1].update(phase="failed", error="Datei ist leer")
    burst["parts"][2].update(phase="ready", text="please describe")

    prompt = render_burst(burst, ["$file", "$text"], inactivity_seconds=45)

    assert prompt.index("Teil 1 · Galerie · $gallery · bereits zugestellt") < prompt.index(
        "Teil 2 · Datei · $file\nNicht übernommen: Datei ist leer"
    ) < prompt.index("Teil 3 · Text · $text\nplease describe")
    assert "Quellereignisse: $gallery, $file, $text" in prompt
    assert "neu: 2, 3" in prompt
    assert "Unterhaltung ab Ereignis $gallery" in prompt
    assert "zeitliche Nähe (höchstens 45 s Pause)" in prompt


def test_prune_keeps_unsettled_bursts():
    bursts: dict = {}
    for index in range(MAX_BURSTS + 5):
        burst = add(bursts, f"$e{index}", "text", index * 100)
        burst["parts"][0]["phase"] = "delivered"
    open_burst = add(bursts, "$open", "gallery", (MAX_BURSTS + 10) * 100)
    prune_bursts(bursts)

    assert len(bursts) <= MAX_BURSTS
    assert open_burst["id"] in bursts
