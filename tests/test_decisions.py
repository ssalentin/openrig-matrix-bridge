import pytest

from openrig_matrix_bridge.decisions import (
    POLL_KIND,
    POLL_START_KEY,
    TEXT_KEY,
    DecisionError,
    normalize_questions,
    poll_response,
    poll_start_content,
)


def question():
    return {
        "id": "goal",
        "question": "Welche Variante?",
        "options": [
            {"id": "small", "label": "Klein", "recommended": True},
            {"id": "full", "label": "Vollständig", "recommended": False},
        ],
    }


def test_normalizes_queue_questions():
    assert normalize_questions([question()]) == [question()]


def test_rejects_question_with_one_option():
    item = question()
    item["options"] = item["options"][:1]
    with pytest.raises(DecisionError, match="between 2 and 4 options"):
        normalize_questions([item])


def test_builds_element_compatible_poll_content():
    content = poll_start_content(question())

    assert content[TEXT_KEY] == "Welche Variante?\n1. Klein\n2. Vollständig"
    assert content[POLL_START_KEY] == {
        "question": {
            TEXT_KEY: "Welche Variante?",
            "body": "Welche Variante?",
            "msgtype": "m.text",
        },
        "kind": POLL_KIND,
        "max_selections": 1,
        "answers": [
            {TEXT_KEY: "Klein", "id": "small"},
            {TEXT_KEY: "Vollständig", "id": "full"},
        ],
    }


def test_reads_unstable_poll_response():
    content = {
        "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"},
        "org.matrix.msc3381.poll.response": {"answers": ["small"]},
    }

    assert poll_response(content) == ("$poll", ["small"])


def test_reads_legacy_poll_response():
    content = {
        "m.relates_to": {"rel_type": "m.reference", "event_id": "$poll"},
        "m.selections": ["full"],
    }

    assert poll_response(content) == ("$poll", ["full"])
