-- D23 규칙 출처: alerts.source, trade_conditions.source 를 agent|chart|user|default 로 통일한다.
--
-- 기존 컬럼과의 관계
-- - alerts.proposal_source(daily_trade 등)는 코치 제안의 세부 분류라 그대로 둔다. 값이 있으면 agent.
-- - alerts.created_via: ai_coach -> agent, chart -> chart, manual·agent_chat -> user,
--   trade_condition -> 연결된 조건주문의 source.
-- - trade_conditions.source: manual -> user, agent -> agent.
-- 'manual' 은 롤아웃 중 이전 버전 pod 의 INSERT 가 실패하지 않도록 CHECK 에 임시로 남긴다.
-- 응답에서는 user 로 바꿔 내보낸다. 안정화 후 별도 마이그레이션에서 제거한다.

-- 1) trade_conditions: 옛 값 manual -> user
ALTER TABLE trade_conditions DROP CONSTRAINT IF EXISTS trade_conditions_source_check;

UPDATE trade_conditions
SET source = 'user'
WHERE source = 'manual';

ALTER TABLE trade_conditions ALTER COLUMN source SET DEFAULT 'user';

DO $$
BEGIN
    ALTER TABLE trade_conditions
        ADD CONSTRAINT trade_conditions_source_check CHECK (
            source IN ('agent', 'chart', 'user', 'default', 'manual')
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- 2) alerts.source 추가 후 백필
ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS source TEXT;

UPDATE alerts
SET source = CASE
    WHEN proposal_source IS NOT NULL OR created_via = 'ai_coach' THEN 'agent'
    WHEN created_via = 'chart' THEN 'chart'
    ELSE 'user'
END
WHERE source IS NULL;

UPDATE alerts AS alert
SET source = CASE WHEN trade_condition.source = 'manual' THEN 'user' ELSE trade_condition.source END
FROM trade_conditions AS trade_condition
WHERE trade_condition.alert_id = alert.id
  AND alert.created_via = 'trade_condition';

ALTER TABLE alerts ALTER COLUMN source SET DEFAULT 'user';
ALTER TABLE alerts ALTER COLUMN source SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE alerts
        ADD CONSTRAINT alerts_source_check CHECK (
            source IN ('agent', 'chart', 'user', 'default')
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
