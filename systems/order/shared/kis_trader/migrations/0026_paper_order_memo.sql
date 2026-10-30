-- 페이퍼 주문 메모 (D21).
--
-- 주문 시트에서 사용자가 남기는 한 줄 메모다. 길이 200자 제한은 API 가
-- 검사한다. 메모는 주문 생성 API 경로에서만 저장하며 Kafka/outbox 주문
-- 메시지 계약에는 싣지 않는다.

ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS memo TEXT;

COMMENT ON COLUMN paper_orders.memo IS
    '사용자가 주문할 때 남긴 메모(200자 이내, API 에서 검사). 없으면 NULL';
