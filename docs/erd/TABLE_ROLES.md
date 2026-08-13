# GOPS 테이블 역할 정리

기준정보와 트랜잭션 원본은 PostgreSQL이 소유하고, 시계열·뉴스·펀더멘털 분석 데이터는 ClickHouse `market_data`가 소유한다. `ch_*`는 ERDCloud에서만 쓰는 표시용 이름이다.

## 사용자·주문·알림

| 테이블 | 저장소 | 역할 | 주요 연결 |
|---|---|---|---|
| `app_users` | PostgreSQL | 내부 사용자 기준정보와 활성 상태 | 모든 사용자 소유 테이블의 `app_user_id` |
| `user_identities` | PostgreSQL | Google 등 외부 인증 주체를 내부 사용자로 매핑 | `app_users` |
| `orders` | PostgreSQL | 실주문 요청 원본과 현재 상태 | 사용자, 종목, 주문 이벤트 |
| `order_events` | PostgreSQL | 주문 상태 변경 이력 | `orders` |
| `idempotency_requests` | PostgreSQL | 같은 주문 API 요청의 중복 생성 차단 | `orders` |
| `outbox_events` | PostgreSQL | DB 커밋과 Kafka 발행 사이의 신뢰성 경계 | 주문 도메인 이벤트 |
| `inbox_events` | PostgreSQL | 소비자별 Kafka 이벤트 중복 처리 차단 | `(consumer_name, event_id)` |
| `broker_submissions` | PostgreSQL | 브로커 제출·응답과 재시도 상태 | `orders` |
| `executions` | PostgreSQL | 실브로커 체결 수신 내역 | `orders` |
| `dlq_events` | PostgreSQL | 처리 실패 메시지와 원본 payload | 재처리 운영 |
| `reconciliation_runs` | PostgreSQL | 내부 주문과 브로커 상태 대사 실행 결과 | 주문 운영 |
| `audit_logs` | PostgreSQL | 관리자·외부 주문 작업 감사 스냅샷 | 외부 `order_id`는 의도적으로 FK 없음 |
| `alerts` | PostgreSQL | 사용자 종목 알림 조건 | 사용자, 종목 |
| `notifications` | PostgreSQL | 알림 발송 결과와 상태 | `alerts` |
| `trade_conditions` | PostgreSQL | 조건 충족 시 모의주문을 실행하는 규칙 | 사용자, alert/`paper_orders`를 통한 종목 연결 |
| `user_notification_preferences` | PostgreSQL | 사용자별 알림 채널·시간 설정 | `app_users` |

## 추천·개인화·주문 코칭

| 테이블 | 저장소 | 역할 | 주요 연결 |
|---|---|---|---|
| `user_recommendation_score_profiles` | PostgreSQL | 추천 점수 가중치 버전 | 사용자 투자 프로필 |
| `user_investment_profiles` | PostgreSQL | 현재 투자 성향과 활성 점수 프로필 | 사용자, 점수 프로필 |
| `user_investment_profile_history` | PostgreSQL | 투자 성향 변경 이력 | 사용자 |
| `user_layout_presets` | PostgreSQL | 사용자 화면 배치 프리셋 | 사용자 |
| `user_portfolio_snapshots` | PostgreSQL | 현재 포트폴리오 분석 스냅샷 | 사용자 |
| `user_portfolio_snapshot_history` | PostgreSQL | 포트폴리오 분석 변경 이력 | 사용자 |
| `trade_decision_check_events` | PostgreSQL | 주문 전 판단 체크 결과 | 사용자, 종목 |
| `order_coach_fill_history` | PostgreSQL | 체결 기반 주문 코칭 이력 | 사용자, 종목, 원천 체결 |
| `stock_recommendation_evidence_snapshots` | PostgreSQL | 추천 시점의 근거 전체 스냅샷 | 사용자, 종목 |
| `stock_recommendation_evidence_candidates` | PostgreSQL | 추천 근거 후보와 채택 여부 | 근거 스냅샷 |
| `stock_recommendation_model_registry` | PostgreSQL | 실제 모델 artifact·버전 레지스트리 | 추천 실행의 nullable 모델 FK |
| `stock_recommendation_runs` | PostgreSQL | 사용자별 추천 계산 실행 단위 | 사용자, 근거, 모델 |
| `stock_recommendation_items` | PostgreSQL | 실행별 추천 종목과 점수 | 추천 실행, 종목 |
| `stock_recommendation_outcomes` | PostgreSQL | 추천 이후 성과·평가 결과 | 추천 항목 |

## 모의투자·종목 기준정보

| 테이블 | 저장소 | 역할 | 주요 연결 |
|---|---|---|---|
| `instruments` | PostgreSQL | 종목의 canonical 식별자·시장·통화 기준정보 | 모든 종목 소유 데이터 |
| `instrument_aliases` | PostgreSQL | 공급자별 심볼을 canonical 종목으로 매핑 | `instruments` |
| `paper_accounts` | PostgreSQL | 사용자별 현재 모의 현금 계좌 | `app_users` |
| `paper_account_runs` | PostgreSQL | 시뮬레이션 실행별 계좌 상태 | 사용자 |
| `paper_positions` | PostgreSQL | 사용자별 종목 현재 보유 집계 | 사용자, 종목 |
| `paper_orders` | PostgreSQL | 모의주문 원본과 체결 집계 상태 | 사용자, 종목 |
| `paper_executions` | PostgreSQL | 부분 체결을 포함한 불변 체결 원장 | `paper_orders` |
| `paper_order_events` | PostgreSQL | 모의주문 상태 변경 이력 | 주문, 선택적 체결 |
| `paper_cash_ledger` | PostgreSQL | 모의 현금 증감 원장 | 주문, 선택적 체결 |
| `simulation_matcher_checkpoints` | PostgreSQL | 시뮬레이션 matcher 재시작 위치 | replay run |

`paper_orders.filled_qty`와 평균 체결가는 조회용 집계다. 금액·수량의 원장은 각각 `paper_cash_ledger`, `paper_executions`가 담당한다.

## 차트 분석 자산

| 테이블 | 저장소 | 역할 | 주요 연결 |
|---|---|---|---|
| `chart_assets.analysis_assets` | PostgreSQL | 분석 결과 자산의 기준 레코드 | 기하 자산 |
| `chart_assets.geometry_assets` | PostgreSQL | 렌더링 가능한 차트 도형 자산 | 분석 자산 |
| `chart_assets.geometry_build_jobs` | PostgreSQL | 도형 빌드 작업 단위 | 빌드 항목 |
| `chart_assets.geometry_build_items` | PostgreSQL | 작업별 종목·구간 빌드 상태 | 빌드 작업, 자산 |
| `chart_assets.geometry_asset_snapshots` | PostgreSQL | 도형 자산 변경 스냅샷 | 기하 자산 |

## ClickHouse 시장·분석 데이터

| ERDCloud 이름 | 실제 테이블 | 역할 | 논리 식별자 |
|---|---|---|---|
| `ch_symbols` | `market_data.symbols` | 공급자 심볼 메타데이터의 분석용 사본 | `instrument_id`, `symbol` |
| `ch_simulation_replay_datasets` | `market_data.simulation_replay_datasets` | 시뮬레이션 데이터셋 카탈로그 | `dataset_id` |
| `ch_simulation_replay_staging` | `market_data.simulation_replay_staging` | replay 입력 정규화 전 staging | dataset, instrument |
| `ch_simulation_replay_events` | `market_data.simulation_replay_events` | replay 원시 이벤트 | dataset, instrument, time |
| `ch_simulation_replay_candles_1m` | `market_data.simulation_replay_candles_1m` | replay 1분 봉 | dataset, instrument, time |
| `ch_trade_ticks` | `market_data.trade_ticks` | 실시간 체결 tick | instrument, event time |
| `ch_quote_ticks` | `market_data.quote_ticks` | 실시간 호가 tick | instrument, event time |
| `ch_chart_candles` | `market_data.chart_candles` | API용 정규화 candle | instrument, interval, time |
| `ch_market_status_events` | `market_data.market_status_events` | 거래소·피드 상태 이벤트 | instrument/market, time |
| `ch_market_events` | `market_data.market_events` | 정규화된 시장 이벤트 | instrument, type, time |
| `ch_agent_graph_expansions` | `market_data.agent_graph_expansions` | 에이전트 종목 관계 확장 결과 | instrument, relation version |
| `ch_news_articles` | `market_data.news_articles` | 종목 뉴스 원문·메타데이터 | instrument, article |
| `ch_news_article_localizations` | `market_data.news_article_localizations` | 뉴스 번역·현지화 결과 | article, locale |
| `ch_news_company_daily_summaries` | `market_data.news_company_daily_summaries` | 종목별 일간 뉴스 요약 | instrument, date, locale |
| `ch_load_audit` | `market_data.load_audit` | 적재 batch 감사와 건수 | batch |
| `ch_backfill_jobs` | `market_data.backfill_jobs` | 시장 데이터 백필 요청 상태 | request, instrument |
| `ch_storage_object_audit` | `market_data.storage_object_audit` | S3 객체 적재·검증 감사 | object path |
| `ch_order_flow_profile_daily` | `market_data.order_flow_profile_daily` | 일간 가격대별 체결량 프로필 | instrument, date, price bin |
| `ch_chart_analysis_assets` | `market_data.chart_analysis_assets` | ClickHouse 조회용 차트 분석 projection | instrument, interval |
| `ch_sec_company_tickers` | `market_data.sec_company_tickers` | SEC CIK와 종목 연결 데이터 | CIK, instrument |
| `ch_sec_filing_events` | `market_data.sec_filing_events` | SEC filing 이벤트 | accession, instrument |
| `ch_sec_raw_artifacts` | `market_data.sec_raw_artifacts` | SEC 원본 artifact 위치·수집 정보 | object path, instrument |
| `ch_sec_financial_facts` | `market_data.sec_financial_facts` | SEC 표준 재무 fact | instrument, metric, period |
| `ch_sec_derived_metrics` | `market_data.sec_derived_metrics` | 재무 fact 파생 지표 | instrument, metric, period |
| `ch_sec_frames` | `market_data.sec_frames` | SEC frame 비교 데이터 | frame, concept, instrument |
| `ch_sec_collection_runs` | `market_data.sec_collection_runs` | SEC 수집 실행 상태 | run |
| `ch_yahoo_earnings_estimates` | `market_data.yahoo_earnings_estimates` | 실적 추정치 시계열 | instrument, metric, period |
| `ch_yahoo_analyst_summaries` | `market_data.yahoo_analyst_summaries` | 애널리스트 요약·톤 | instrument, collected time |
| `ch_company_journal_reports_v1` | `market_data.company_journal_reports_v1` | 기업 저널 보고서 물리 버전 | instrument, report version |
| `ch_company_journal_generation_events_v1` | `market_data.company_journal_generation_events_v1` | 기업 저널 생성 이벤트 물리 버전 | instrument, event |

ClickHouse의 `instrument_id`는 PostgreSQL `instruments`를 가리키는 논리 관계이며 FK가 아니다. `ReplacingMergeTree` 조회는 대응하는 `*_latest` View를 우선 사용한다. 기업 저널 API는 버전 없는 `company_journal_reports`, `company_journal_generation_events` View를 사용한다.
