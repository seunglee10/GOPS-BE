-- 투자 복기의 주문별 메모와 습관 (D27).
--
-- 주문할 때 남긴 메모는 paper_orders.memo(0026)에 그대로 둔다. 이 테이블은 복기
-- 화면에서 사용자가 나중에 고쳐 쓴 메모와, 서버가 붙인 습관 분류를 사용자가 바꾼
-- 값을 담는다. 월간 리포트는 여기 메모가 있으면 그것을, 없으면 주문 메모를 보여 준다.
-- 습관은 habit_source = 'user' 일 때만 서버 분류보다 우선한다.

CREATE TABLE IF NOT EXISTS order_notes (
    order_id TEXT PRIMARY KEY REFERENCES paper_orders(order_id) ON DELETE CASCADE,
    app_user_id UUID REFERENCES app_users(app_user_id),
    memo TEXT,
    habit TEXT,
    habit_source TEXT NOT NULL DEFAULT 'auto',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT order_notes_habit_check
        CHECK (habit IS NULL OR habit IN ('plan', 'chase', 'rush', 'avg', 'news', 'exit')),
    CONSTRAINT order_notes_habit_source_check
        CHECK (habit_source IN ('auto', 'user')),
    CONSTRAINT order_notes_memo_length_check
        CHECK (memo IS NULL OR char_length(memo) <= 200)
);

CREATE INDEX IF NOT EXISTS idx_order_notes_app_user ON order_notes (app_user_id)
    WHERE app_user_id IS NOT NULL;

COMMENT ON TABLE order_notes IS
    '투자 복기에서 사용자가 고친 주문 메모와 습관. 소유자는 paper_orders.user_id 로 확인한다';
COMMENT ON COLUMN order_notes.memo IS
    '복기 때 고쳐 쓴 메모(200자 이내). NULL 이면 paper_orders.memo 를 보여 준다';
COMMENT ON COLUMN order_notes.habit IS
    '습관 분류 plan·chase·rush·avg·news·exit. NULL 이면 서버 분류를 쓴다';
COMMENT ON COLUMN order_notes.habit_source IS
    'user 면 사용자가 고른 값이라 서버 분류보다 우선, auto 면 서버 분류를 쓴다';
