-- 학습 코스 진행 (D30).
--
-- 코스와 문제는 API 서버의 JSON 시드(app/learn/data/<course_id>.json)에 있고 DB 에는 없다.
-- 그래서 course_id·question_id 는 FK 없이 텍스트로 둔다. 시드에서 문제가 빠지면
-- API 가 응답할 때 걸러 낸다.
-- 맞힌 문제만 한 줄씩 남긴다. 틀린 시도와 힌트 사용은 기록하지 않는다.

CREATE TABLE IF NOT EXISTS learn_progress (
    app_user_id UUID NOT NULL REFERENCES app_users(app_user_id) ON DELETE CASCADE,
    course_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    solved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (app_user_id, course_id, question_id)
);

COMMENT ON TABLE learn_progress IS
    '사용자가 맞힌 코스 문제. 맞힌 문제만 저장하고 처음 맞힌 시각을 남긴다.';
COMMENT ON COLUMN learn_progress.course_id IS
    '코스 시드 파일 이름(예: chart-basics). FK 없음.';
COMMENT ON COLUMN learn_progress.question_id IS
    '시드 안의 문제 id(예: c1, t1). FK 없음.';
COMMENT ON COLUMN learn_progress.solved_at IS
    '처음 맞힌 시각. 다시 맞혀도 바꾸지 않는다.';
