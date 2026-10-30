"""사용자 리스크 설정 — 성향 한도 조절 + 일일 매수 예산.

설계 원칙 (risk-user-settings-plan.md):
- 조절 가능한 것은 성향 한도(비중·섹터·일일 손실·예산)뿐. 실수 방지(팻핑거)와
  감지 임계(세력·상관)는 닫혀 있다.
- 비대칭 적용: 조이는 변경(값 하향, 예산 신규 설정)은 즉시, 푸는 변경(값 상향,
  예산 해제)은 다음 날부터(pending). 자기구속 장치는 풀기 어려워야 한다.
- 가드레일 밖 값은 거부. 한도 무력화는 불가.
- 저장: Redis(`gops:risk:settings:<user>`) 또는 인메모리 폴백. 변경 이력 50건.
- 주문 전 점검 가드레일(D26)도 같은 저장소·같은 비대칭 규칙을 쓴다. 엔진(RiskConfig)에는
  넘기지 않고 `app.services.pretrade_guardrails`가 모의 주문 사전 점검에서만 읽는다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

SETTINGS_KEY_PREFIX = "gops:risk:settings"
BUDGET_USED_KEY_PREFIX = "gops:risk:budget-used"
BUDGET_USED_TTL_SECONDS = 3 * 24 * 3600
HISTORY_LIMIT = 50

# API(camelCase) <-> RiskConfig(snake_case)
ADJUSTABLE_FIELDS = {
    "singleNameMaxWeight": "single_name_max_weight",
    "sectorMaxWeight": "sector_max_weight",
    "dailyLossLimitPct": "daily_loss_limit_pct",
    "dailyBuyBudget": "daily_buy_budget",
}

# 가드레일 (min, max). 예산은 0 허용(오늘 매수 금지), 상한 없음.
GUARDRAILS: dict[str, tuple[Decimal, Decimal | None]] = {
    "singleNameMaxWeight": (Decimal("0.05"), Decimal("0.50")),
    "sectorMaxWeight": (Decimal("0.10"), Decimal("0.80")),
    "dailyLossLimitPct": (Decimal("0.01"), Decimal("0.10")),
    "dailyBuyBudget": (Decimal("0"), None),
}

DEFAULTS = {
    "singleNameMaxWeight": Decimal("0.20"),
    "sectorMaxWeight": Decimal("0.40"),
    "dailyLossLimitPct": Decimal("0.03"),
    "dailyBuyBudget": None,
}

# D26 주문 전 점검 가드레일. 엔진 한도(ADJUSTABLE_FIELDS)와 따로 둔다.
# 퍼센트 값은 0.0~100.0 단위(3.0 = 3%). None은 끔.
GUARDRAIL_DEFAULTS: dict[str, Any] = {
    "chaseBuyWarnPercent": Decimal("3.0"),
    "blockAverageDownWithoutStop": True,
    "confirmSellOnDropPercent": Decimal("2.0"),
    "maxPositionWeightPercent": Decimal("25"),
}
GUARDRAIL_RANGES: dict[str, tuple[Decimal, Decimal]] = {
    "chaseBuyWarnPercent": (Decimal("0.5"), Decimal("30")),
    "confirmSellOnDropPercent": (Decimal("0.5"), Decimal("30")),
    "maxPositionWeightPercent": (Decimal("5"), Decimal("100")),
}
GUARDRAIL_NULLABLE = {"chaseBuyWarnPercent", "confirmSellOnDropPercent"}
GUARDRAIL_BOOLEANS = {"blockAverageDownWithoutStop"}

PRESETS = {
    "conservative": {
        "singleNameMaxWeight": Decimal("0.15"),
        "sectorMaxWeight": Decimal("0.30"),
        "dailyLossLimitPct": Decimal("0.02"),
    },
    "standard": {
        "singleNameMaxWeight": Decimal("0.20"),
        "sectorMaxWeight": Decimal("0.40"),
        "dailyLossLimitPct": Decimal("0.03"),
    },
    "aggressive": {
        "singleNameMaxWeight": Decimal("0.30"),
        "sectorMaxWeight": Decimal("0.50"),
        "dailyLossLimitPct": Decimal("0.05"),
    },
}


class RiskSettingsError(ValueError):
    """Invalid settings input (guardrail violation, unknown field/preset)."""

    def __init__(self, message: str, *, code: str = "invalid_risk_setting", field: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field

    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.field:
            detail["field"] = self.field
        return detail


@dataclass(frozen=True)
class SettingsChangeResult:
    applied_now: dict[str, Any]
    scheduled: dict[str, Any]
    effective_date: str | None


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def next_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


class RiskSettingsStore:
    """사용자별 설정 저장 + 비대칭 적용 + pending 승격."""

    def __init__(self, redis_client: Any = None):
        self._redis = redis_client
        self._memory: dict[str, dict[str, Any]] = {}

    # --- read -----------------------------------------------------------------

    def resolve(self, user_sub: str, *, today: str | None = None) -> dict[str, Any]:
        """pending 승격까지 반영한 현재 상태 반환."""
        today = today or utc_today()
        state = self._load(user_sub)
        pending = state.get("pending")
        if pending and str(pending.get("effectiveDate") or "") <= today:
            state["active"] = {**state.get("active", {}), **pending.get("values", {})}
            state["pending"] = None
            self._save(user_sub, state)
        return state

    def active_values(self, user_sub: str, *, today: str | None = None) -> dict[str, Any]:
        """디폴트 위에 사용자 active를 병합한 최종 값 (API 필드명 기준)."""
        state = self.resolve(user_sub, today=today)
        merged = {**DEFAULTS, **GUARDRAIL_DEFAULTS}
        for key, value in (state.get("active") or {}).items():
            if key in ADJUSTABLE_FIELDS or key in GUARDRAIL_DEFAULTS:
                merged[key] = _parse_stored(key, value)
        return merged

    def engine_overrides(self, user_sub: str, *, today: str | None = None) -> dict[str, Any]:
        """RiskConfig 오버라이드(snake_case) — 디폴트와 같은 값은 굳이 넘기지 않음."""
        overrides: dict[str, Any] = {}
        for api_key, value in self.active_values(user_sub, today=today).items():
            if api_key in ADJUSTABLE_FIELDS and value != DEFAULTS[api_key]:
                overrides[ADJUSTABLE_FIELDS[api_key]] = value
        return overrides

    # --- write ----------------------------------------------------------------

    def apply_change(
        self,
        user_sub: str,
        *,
        values: dict[str, Any] | None = None,
        preset: str | None = None,
        today: str | None = None,
    ) -> SettingsChangeResult:
        today = today or utc_today()
        requested = self._expand_request(values, preset)
        current = self.active_values(user_sub, today=today)

        applied_now: dict[str, Any] = {}
        scheduled: dict[str, Any] = {}
        for key, new_value in requested.items():
            if new_value == current[key]:
                continue
            if _is_tightening(key, current[key], new_value):
                applied_now[key] = new_value
            else:
                scheduled[key] = new_value

        state = self._load(user_sub)
        active = dict(state.get("active") or {})
        for key, value in applied_now.items():
            active[key] = _serialize(value)
        state["active"] = active

        effective_date: str | None = None
        if scheduled:
            effective_date = next_day(today)
            pending_values = dict((state.get("pending") or {}).get("values") or {})
            for key, value in scheduled.items():
                pending_values[key] = _serialize(value)
            state["pending"] = {"effectiveDate": effective_date, "values": pending_values}

        history = list(state.get("history") or [])
        for key, value in {**applied_now, **scheduled}.items():
            history.insert(0, {
                "at": datetime.now(timezone.utc).isoformat(),
                "field": key,
                "from": _serialize(current[key]),
                "to": _serialize(value),
                "appliedAt": "immediate" if key in applied_now else effective_date,
            })
        state["history"] = history[:HISTORY_LIMIT]

        self._save(user_sub, state)
        return SettingsChangeResult(applied_now=applied_now, scheduled=scheduled, effective_date=effective_date)

    # --- internals --------------------------------------------------------------

    def _expand_request(self, values: dict[str, Any] | None, preset: str | None) -> dict[str, Any]:
        requested: dict[str, Any] = {}
        if preset is not None:
            if preset not in PRESETS:
                raise RiskSettingsError(f"unknown preset: {preset} (conservative|standard|aggressive)", code="unknown_preset")
            requested.update(PRESETS[preset])
        for key, raw in (values or {}).items():
            if key not in ADJUSTABLE_FIELDS and key not in GUARDRAIL_DEFAULTS:
                raise RiskSettingsError(f"not adjustable: {key}", code="unknown_risk_setting", field=key)
            requested[key] = _validate_value(key, raw)
        if not requested:
            raise RiskSettingsError("nothing to change: provide preset or values", code="empty_risk_settings_change")
        return requested

    def _load(self, user_sub: str) -> dict[str, Any]:
        if self._redis is not None:
            try:
                raw = self._redis.get(f"{SETTINGS_KEY_PREFIX}:{user_sub}")
                if raw:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        return loaded
            except Exception:
                pass
        return json.loads(json.dumps(self._memory.get(user_sub) or {"active": {}, "pending": None, "history": []}))

    def _save(self, user_sub: str, state: dict[str, Any]) -> None:
        self._memory[user_sub] = state
        if self._redis is not None:
            try:
                self._redis.set(f"{SETTINGS_KEY_PREFIX}:{user_sub}", json.dumps(state, ensure_ascii=False))
            except Exception:
                pass


class RiskBudgetTracker:
    """일자별 매수 누적액 — 주문 접수(202) 기준의 보수적 근사."""

    def __init__(self, redis_client: Any = None):
        self._redis = redis_client
        self._memory: dict[str, float] = {}

    def record_buy(self, user_sub: str, amount: Decimal, *, day: str | None = None) -> None:
        if amount <= 0:
            return
        key = self._key(user_sub, day)
        if self._redis is not None:
            try:
                self._redis.incrbyfloat(key, float(amount))
                self._redis.expire(key, BUDGET_USED_TTL_SECONDS)
                return
            except Exception:
                pass
        self._memory[key] = self._memory.get(key, 0.0) + float(amount)

    def used_today(self, user_sub: str, *, day: str | None = None) -> Decimal:
        key = self._key(user_sub, day)
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
                if raw is not None:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    return Decimal(str(raw))
            except Exception:
                pass
        return Decimal(str(self._memory.get(key, 0.0)))

    def _key(self, user_sub: str, day: str | None) -> str:
        return f"{BUDGET_USED_KEY_PREFIX}:{user_sub}:{day or utc_today()}"


# --- app wiring -----------------------------------------------------------------


def settings_store_from_app(app: Any) -> RiskSettingsStore:
    existing = getattr(app.state, "risk_settings_store", None)
    if existing is not None:
        return existing
    store = RiskSettingsStore(_redis_client())
    app.state.risk_settings_store = store
    return store


def budget_tracker_from_app(app: Any) -> RiskBudgetTracker:
    existing = getattr(app.state, "risk_budget_tracker", None)
    if existing is not None:
        return existing
    tracker = RiskBudgetTracker(_redis_client())
    app.state.risk_budget_tracker = tracker
    return tracker


def _redis_client():
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis

        return redis.from_url(url, decode_responses=True)
    except Exception:
        return None


# --- value helpers ----------------------------------------------------------------


def _validate_value(key: str, raw: Any) -> Any:
    if key in GUARDRAIL_DEFAULTS:
        return _validate_guardrail(key, raw)
    if key == "dailyBuyBudget" and raw is None:
        return None  # 예산 끄기 (풀기 취급 → 다음 날 적용)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise RiskSettingsError(f"{key} must be a number", code="invalid_risk_setting", field=key) from exc
    minimum, maximum = GUARDRAILS[key]
    if value < minimum or (maximum is not None and value > maximum):
        bound = f">= {minimum}" if maximum is None else f"{minimum} ~ {maximum}"
        raise RiskSettingsError(f"{key} out of range ({bound})", code="risk_setting_out_of_range", field=key)
    return value


def _validate_guardrail(key: str, raw: Any) -> Decimal | bool | None:
    if key in GUARDRAIL_BOOLEANS:
        if not isinstance(raw, bool):
            raise RiskSettingsError(f"{key} must be true or false", code="invalid_risk_setting", field=key)
        return raw
    if raw is None:
        if key in GUARDRAIL_NULLABLE:
            return None
        raise RiskSettingsError(f"{key} cannot be null", code="invalid_risk_setting", field=key)
    if isinstance(raw, bool):
        raise RiskSettingsError(f"{key} must be a number", code="invalid_risk_setting", field=key)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise RiskSettingsError(f"{key} must be a number", code="invalid_risk_setting", field=key) from exc
    if not value.is_finite():
        raise RiskSettingsError(f"{key} must be a number", code="invalid_risk_setting", field=key)
    minimum, maximum = GUARDRAIL_RANGES[key]
    if value < minimum or value > maximum:
        raise RiskSettingsError(f"{key} out of range ({minimum} ~ {maximum})", code="risk_setting_out_of_range", field=key)
    return value


def _is_tightening(key: str, current: Any, new: Any) -> bool:
    """조이는 변경인가? 조임=즉시, 풀기=다음 날."""
    if key in GUARDRAIL_BOOLEANS:
        return new is True  # 켜기 = 조임, 끄기 = 풀기
    if key in GUARDRAIL_NULLABLE:
        if current is None:
            return new is not None  # 꺼진 점검을 켜기 = 조임
        if new is None:
            return False  # 끄기 = 풀기
        return new < current  # 기준을 낮추면 더 자주 걸린다 = 조임
    if key == "dailyBuyBudget":
        if current is None:
            return new is not None  # 예산 신규 설정 = 조임
        if new is None:
            return False  # 예산 해제 = 풀기
        return new < current
    # 퍼센트 한도: 낮을수록 조임
    return new is not None and current is not None and new < current


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    return str(value)


def _parse_stored(key: str, value: Any) -> Any:
    if key in GUARDRAIL_BOOLEANS:
        return value if isinstance(value, bool) else GUARDRAIL_DEFAULTS[key]
    if value is None:
        return None if key in GUARDRAIL_NULLABLE or key in DEFAULTS else GUARDRAIL_DEFAULTS[key]
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return {**DEFAULTS, **GUARDRAIL_DEFAULTS}[key]


def serialize_values(values: dict[str, Any]) -> dict[str, Any]:
    """API 응답 값. 엔진 한도는 기존처럼 문자열, D26 가드레일은 계약대로 숫자·불리언."""
    return {
        key: (_guardrail_json(value) if key in GUARDRAIL_DEFAULTS else _serialize(value))
        for key, value in values.items()
    }


def _guardrail_json(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    number = float(value)
    return int(number) if number.is_integer() and not isinstance(value, float) and "." not in str(value) else number


def guardrail_values(values: dict[str, Any]) -> dict[str, Any]:
    """active_values() 중 D26 가드레일만 꺼낸다 (Decimal·bool·None 그대로)."""
    return {key: values.get(key, default) for key, default in GUARDRAIL_DEFAULTS.items()}
