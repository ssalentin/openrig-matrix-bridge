from __future__ import annotations

from typing import Any

POLL_START_TYPE = "org.matrix.msc3381.poll.start"
POLL_RESPONSE_TYPE = "org.matrix.msc3381.poll.response"
STABLE_POLL_START_TYPE = "m.poll.start"
STABLE_POLL_RESPONSE_TYPE = "m.poll.response"
POLL_START_KEY = "org.matrix.msc3381.poll.start"
POLL_RESPONSE_KEY = "org.matrix.msc3381.poll.response"
POLL_KIND = "org.matrix.msc3381.poll.disclosed"
TEXT_KEY = "org.matrix.msc1767.text"


class DecisionError(ValueError):
    pass


def normalize_questions(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 4:
        raise DecisionError("a decision may contain between 0 and 4 questions")
    normalized: list[dict[str, Any]] = []
    question_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise DecisionError("each question must be an object")
        question_id = str(item.get("id") or "").strip()
        question = str(item.get("question") or "").strip()
        options_raw = item.get("options")
        if not question_id or not question:
            raise DecisionError("each question needs a non-empty id and question")
        if question_id in question_ids:
            raise DecisionError(f"duplicate question id: {question_id}")
        question_ids.add(question_id)
        if not isinstance(options_raw, list) or not 2 <= len(options_raw) <= 4:
            raise DecisionError("each question needs between 2 and 4 options")
        options: list[dict[str, Any]] = []
        option_ids: set[str] = set()
        recommended_count = 0
        for option in options_raw:
            if not isinstance(option, dict):
                raise DecisionError("each option must be an object")
            option_id = str(option.get("id") or "").strip()
            label = str(option.get("label") or "").strip()
            if not option_id or not label:
                raise DecisionError("each option needs a non-empty id and label")
            if option_id in option_ids:
                raise DecisionError(f"duplicate option id for {question_id}: {option_id}")
            if len(label) > 75:
                raise DecisionError("option labels must be at most 75 characters")
            option_ids.add(option_id)
            recommended = bool(option.get("recommended", False))
            recommended_count += int(recommended)
            options.append(
                {"id": option_id, "label": label, "recommended": recommended}
            )
        if recommended_count > 1:
            raise DecisionError("at most one option per question may be recommended")
        normalized.append(
            {"id": question_id, "question": question, "options": options}
        )
    return normalized


def poll_start_content(question: dict[str, Any]) -> dict[str, Any]:
    answers = [
        {TEXT_KEY: option["label"], "id": option["id"]}
        for option in question["options"]
    ]
    fallback = "\n".join(
        [question["question"]]
        + [
            f"{index}. {option['label']}"
            for index, option in enumerate(question["options"], start=1)
        ]
    )
    return {
        TEXT_KEY: fallback,
        POLL_START_KEY: {
            "question": {
                TEXT_KEY: question["question"],
                "body": question["question"],
                "msgtype": "m.text",
            },
            "kind": POLL_KIND,
            "max_selections": 1,
            "answers": answers,
        },
    }


def poll_response(content: dict[str, Any]) -> tuple[str, list[str]] | None:
    relation = content.get("m.relates_to")
    if not isinstance(relation, dict) or relation.get("rel_type") != "m.reference":
        return None
    poll_event_id = relation.get("event_id")
    if not isinstance(poll_event_id, str):
        return None
    response = content.get(POLL_RESPONSE_KEY)
    answers: Any = response.get("answers") if isinstance(response, dict) else None
    if answers is None:
        response = content.get("m.selections")
        answers = response
    if not isinstance(answers, list) or not all(isinstance(item, str) for item in answers):
        return None
    return poll_event_id, answers
