import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MARKET_SHARED = ROOT / "systems" / "market-data" / "shared"
ORDER_SHARED = ROOT / "systems" / "order" / "shared"
BACKEND = ROOT / "systems" / "api-server" / "pods" / "api-server"
for path in (str(MARKET_SHARED), str(ORDER_SHARED), str(BACKEND), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi.testclient import TestClient  # noqa: E402

from app.auth.dependencies import require_current_user  # noqa: E402
from app.auth.models import AuthenticatedUser  # noqa: E402
from app.learn.courses import DATA_DIR, check_answer, load_course  # noqa: E402
from app.learn.repository import InMemoryLearnProgressRepository, LearnProgressRepositoryError  # noqa: E402
from app.main import create_app  # noqa: E402

FE_QUESTION_KEYS = {
    "id", "day", "topic", "title", "kind", "prompt", "opts", "answer", "answerSet", "band",
    "tolerance", "count", "bridge", "fig", "hints", "explanation",
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


class BrokenRepository:
    def list_solved(self, *args):
        raise LearnProgressRepositoryError("down")

    def mark_solved(self, *args):
        raise LearnProgressRepositoryError("down")


def user(sub: str) -> AuthenticatedUser:
    return AuthenticatedUser(sub=sub, email=f"{sub}@gops.local", email_verified=True, app_user_id=f"uid-{sub}")


class CourseSeedTest(unittest.TestCase):
    def setUp(self):
        self.course = load_course("chart-basics")

    def test_seed_matches_frontend_course_payload_shape(self):
        self.assertEqual(self.course["courseId"], "chart-basics")
        self.assertEqual(self.course["titleKo"], "차트 읽기 첫걸음")
        self.assertEqual([d["day"] for d in self.course["days"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(len(self.course["questions"]), 15)
        for q in self.course["questions"]:
            self.assertTrue(FE_QUESTION_KEYS <= set(q), q["id"])
            self.assertIsInstance(q["explanation"], str)
            if q["kind"] == "choice":
                self.assertIsInstance(q["answer"], int)
                self.assertLess(q["answer"], len(q["opts"]))
            if q["kind"] == "tap":
                self.assertTrue(q["answerSet"])
            if q["kind"] == "level":
                self.assertEqual(len(q["band"]), 2)
            chart = q["fig"].get("chart")
            if chart:
                self.assertIn(chart, self.course["charts"])

    def test_function_answers_are_pre_evaluated(self):
        by_id = {q["id"]: q for q in self.course["questions"]}
        self.assertEqual(by_id["c3"]["answerSet"], [1, 6, 8, 11])
        self.assertEqual(by_id["t1"]["answerSet"], [26, 38, 51])
        nvda = self.course["charts"]["nvda"]["rows"]
        self.assertEqual(nvda[by_id["v1"]["answerSet"][0]][0], "2023-05-25")
        low = min(r[3] for r in self.course["charts"]["ko"]["rows"])
        self.assertAlmostEqual(by_id["s1"]["band"][0], low * 0.994)
        self.assertIn("3.8배", by_id["v3"]["explanation"])
        self.assertEqual(len(by_id["c2"]["hints"]), 2)

    def test_check_answer_rules(self):
        by_id = {q["id"]: q for q in self.course["questions"]}
        self.assertTrue(check_answer(by_id["c1"], 0))
        self.assertFalse(check_answer(by_id["c1"], 1))
        self.assertTrue(check_answer(by_id["c3"], [11, 1, 8, 6]))
        self.assertFalse(check_answer(by_id["c3"], [1, 6, 8]))
        self.assertTrue(check_answer(by_id["t1"], [25, 39, 51]))  # 하루 앞뒤 허용
        self.assertFalse(check_answer(by_id["t1"], [24, 38, 51]))
        self.assertTrue(check_answer(by_id["s1"], 58.4))
        self.assertFalse(check_answer(by_id["s1"], 61.0))

    def test_seed_file_is_valid_json(self):
        json.loads((DATA_DIR / "chart-basics.json").read_text(encoding="utf-8"))


class LearnRoutesTest(unittest.TestCase):
    def setUp(self):
        os.environ["AUTH_ENABLED"] = "false"
        self.app = create_app()
        self.app.state.learn_progress_repository = InMemoryLearnProgressRepository()
        self.app.state.learn_now_provider = Clock()
        self.users = {"current": user("alice")}
        self.app.dependency_overrides[require_current_user] = lambda: self.users["current"]
        self.client = TestClient(self.app)

    def post(self, question_id, correct=True, **extra):
        return self.client.post(
            "/api/learn/progress",
            json={"courseId": "chart-basics", "questionId": question_id, "correct": correct, **extra},
        )

    def test_get_course_is_public(self):
        self.app.dependency_overrides.clear()
        os.environ["AUTH_ENABLED"] = "true"
        try:
            response = self.client.get("/api/learn/courses/chart-basics")
        finally:
            os.environ["AUTH_ENABLED"] = "false"
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["questions"]), 15)

    def test_unknown_course_is_404(self):
        for course_id in ("nope", "..chart-basics", "Chart-Basics"):
            response = self.client.get(f"/api/learn/courses/{course_id}")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["detail"]["code"], "course_not_found")
        response = self.client.get("/api/learn/progress", params={"courseId": "nope"})
        self.assertEqual(response.json()["detail"]["code"], "course_not_found")

    def test_progress_starts_empty(self):
        response = self.client.get("/api/learn/progress", params={"courseId": "chart-basics"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"solved": [], "total": 15})

    def test_only_correct_answers_are_stored(self):
        self.assertEqual(self.post("c1", True).json()["solved"], ["c1"])
        wrong = self.post("c2", False)
        self.assertEqual(wrong.status_code, 200)
        self.assertEqual(wrong.json(), {"solved": ["c1"], "total": 15, "correct": False})
        self.post("t1", True)
        self.post("c1", True)  # 다시 맞혀도 한 번만
        response = self.client.get("/api/learn/progress", params={"courseId": "chart-basics"})
        self.assertEqual(response.json()["solved"], ["c1", "t1"])

    def test_server_rechecks_when_answer_given(self):
        lied = self.post("c1", True, answer=2)
        self.assertEqual(lied.json()["correct"], False)
        self.assertEqual(lied.json()["solved"], [])
        honest = self.post("c3", False, answer=[1, 6, 8, 11])
        self.assertEqual(honest.json()["correct"], True)
        self.assertEqual(honest.json()["solved"], ["c3"])
        level = self.post("s1", True, answer=58.4)
        self.assertEqual(level.json()["solved"], ["c3", "s1"])

    def test_answer_shape_mismatch_is_422(self):
        response = self.post("c3", True, answer=1)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "invalid_answer")

    def test_unknown_question_is_422_and_unknown_course_404(self):
        response = self.post("zz", True)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "question_not_found")
        response = self.client.post(
            "/api/learn/progress", json={"courseId": "nope", "questionId": "c1", "correct": True}
        )
        self.assertEqual(response.status_code, 404)

    def test_progress_is_per_user(self):
        self.post("c1", True)
        self.users["current"] = user("bob")
        response = self.client.get("/api/learn/progress", params={"courseId": "chart-basics"})
        self.assertEqual(response.json()["solved"], [])

    def test_storage_failure_is_503(self):
        self.app.state.learn_progress_repository = BrokenRepository()
        response = self.client.get("/api/learn/progress", params={"courseId": "chart-basics"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "learn_progress_unavailable")
        self.assertEqual(self.post("c1").status_code, 503)


if __name__ == "__main__":
    unittest.main()
