"""학습 API. D29 코스(공개), D30 코스 진행(로그인 필요)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.dependencies import require_current_user
from app.auth.models import AuthenticatedUser
from app.learn.courses import InvalidAnswer, check_answer, find_question, load_course, question_ids
from app.learn.repository import LearnProgressRepositoryError, learn_progress_repository_from_app

router = APIRouter(prefix="/api/learn", tags=["learn"])


class ProgressBody(BaseModel):
    courseId: str = Field(min_length=1, max_length=64)
    questionId: str = Field(min_length=1, max_length=64)
    correct: bool
    # 주면 서버가 시드로 다시 채점하고 그 결과를 쓴다. choice=번호, tap=번호 목록, level=가격.
    answer: int | float | list[int | float] | None = None


def _course_or_404(course_id: str) -> dict[str, Any]:
    course = load_course(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail={"code": "course_not_found"})
    return course


def _user_key(user: AuthenticatedUser) -> str:
    return str(user.app_user_id or user.sub)


def _now(app: Any) -> datetime:
    provider: Callable[[], datetime] | None = getattr(app.state, "learn_now_provider", None)
    return provider() if callable(provider) else datetime.now(timezone.utc)


def _progress(course: dict[str, Any], solved: list[str]) -> dict[str, Any]:
    known = set(question_ids(course))
    return {"solved": [qid for qid in solved if qid in known], "total": len(known)}


def _repository(request: Request):
    try:
        return learn_progress_repository_from_app(request.app)
    except LearnProgressRepositoryError as exc:
        raise HTTPException(status_code=503, detail={"code": "learn_progress_unavailable"}) from exc


@router.get("/courses/{course_id}")
def get_course(course_id: str) -> dict[str, Any]:
    return _course_or_404(course_id)


@router.get("/progress")
def get_progress(
    request: Request,
    course_id: str = Query(alias="courseId", min_length=1, max_length=64),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    course = _course_or_404(course_id)
    try:
        solved = _repository(request).list_solved(_user_key(user), course_id)
    except LearnProgressRepositoryError as exc:
        raise HTTPException(status_code=503, detail={"code": "learn_progress_unavailable"}) from exc
    return _progress(course, solved)


@router.post("/progress")
def post_progress(
    body: ProgressBody,
    request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, Any]:
    course = _course_or_404(body.courseId)
    question = find_question(course, body.questionId)
    if question is None:
        raise HTTPException(status_code=422, detail={"code": "question_not_found"})
    correct = body.correct
    if body.answer is not None:
        try:
            correct = check_answer(question, body.answer)
        except InvalidAnswer as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_answer"}) from exc
    repository = _repository(request)
    key = _user_key(user)
    try:
        if correct:
            repository.mark_solved(key, body.courseId, body.questionId, _now(request.app))
        solved = repository.list_solved(key, body.courseId)
    except LearnProgressRepositoryError as exc:
        raise HTTPException(status_code=503, detail={"code": "learn_progress_unavailable"}) from exc
    return {**_progress(course, solved), "correct": correct}
