"""코스 시드(D29). app/learn/data/<courseId>.json 을 읽고 정답을 판정한다.

시드는 scripts/learn/convert_mock_course.mjs 로 목업에서 만든다. 손으로 고치지 않는다.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
COURSE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


@lru_cache(maxsize=16)
def load_course(course_id: str) -> dict[str, Any] | None:
    if not COURSE_ID_PATTERN.fullmatch(course_id):
        return None
    path = DATA_DIR / f"{course_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def find_question(course: dict[str, Any], question_id: str) -> dict[str, Any] | None:
    for question in course.get("questions", []):
        if question.get("id") == question_id:
            return question
    return None


def question_ids(course: dict[str, Any]) -> list[str]:
    return [str(q["id"]) for q in course.get("questions", [])]


class InvalidAnswer(ValueError):
    """답의 모양이 문제 종류와 맞지 않을 때."""


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidAnswer("answer must be a finite number")
    return float(value)


def check_answer(question: dict[str, Any], answer: Any) -> bool:
    """프론트 isCorrect 와 같은 규칙. choice=보기 번호, tap=고른 날 번호 목록, level=가격."""
    kind = question.get("kind")
    if kind == "choice":
        return _number(answer) == question.get("answer")
    if kind == "level":
        band = question.get("band") or []
        if len(band) != 2:
            return False
        price = _number(answer)
        return float(band[0]) <= price <= float(band[1])
    if kind == "tap":
        if not isinstance(answer, list):
            raise InvalidAnswer("tap answer must be a list of row indexes")
        picked = [_number(a) for a in answer]
        expected = [float(a) for a in question.get("answerSet") or []]
        tol = float(question.get("tolerance") or 0)
        return (
            len(picked) == len(expected)
            and all(any(abs(e - p) <= tol for e in expected) for p in picked)
            and all(any(abs(e - p) <= tol for p in picked) for e in expected)
        )
    raise InvalidAnswer(f"unknown question kind: {kind}")
