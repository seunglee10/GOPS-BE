from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from gops_simul.config import PROJECT_ROOT


SCENARIO_ID = "saturday-demo-amd-iff-oke"
SYMBOLS = ("AMD", "OKE")
SEED_PRICES = {"AMD": 565.0, "OKE": 90.0}
PHASES = (
    ("market-overview", "시장 조망", 0, "AMD와 OKE의 자연스러운 체결·호가 흐름을 관찰합니다."),
    ("breaking-event", "지정학 이벤트", 210, "반도체 약세와 에너지 강세에 대응합니다."),
    ("market-close", "장 마감·복기", 285, "주문·알림 상태와 오늘의 판단을 리포트로 복기합니다."),
)


def main() -> None:
    output = PROJECT_ROOT / "data" / "scenarios" / SCENARIO_ID
    output.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    manifest = {
        "scenarioId": SCENARIO_ID,
        "title": "Saturday operator demo · AMD, OKE",
        "durationSeconds": 300,
        "breakingNewsAtSeconds": 210,
        "seedPrices": SEED_PRICES,
        "symbols": list(SYMBOLS),
        "phases": [
            {"id": phase_id, "label": label, "atSeconds": at_seconds, "summary": summary}
            for phase_id, label, at_seconds, summary in PHASES
        ],
        "source": {
            "provider": "GOPS deterministic scenario generator",
            "feed": "simulation",
            "marketSession": "regular",
            "synthetic": True,
            "generatedEventCount": len(rows),
            "notice": "시연 전용 합성 체결·호가이며 실제 시장 데이터가 아닙니다.",
        },
        "recommendations": [
            {
                "symbol": "AMD", "rank": 1, "score": 84, "confidence": 0.79,
                "sector": "Information Technology", "changePercent": 0.6,
                "reasons": ["거래량 회복", "지지 구간 재확인"],
                "riskWarnings": ["반도체 집중 위험"],
            },
            {
                "symbol": "OKE", "rank": 2, "score": 78, "confidence": 0.74,
                "sector": "Energy", "changePercent": 0.2,
                "reasons": ["에너지 업종 분산", "이벤트 헤지 후보"],
                "riskWarnings": ["뉴스 민감도 높음"],
            },
        ],
        "chartAnalysis": {
            "symbol": "AMD", "pattern": "이벤트 대응 패턴", "support": 525.50,
            "resistance": 566.20, "entry": 530.50, "stop": 524.00,
            "summary": "알림 후 5초간 가격을 유지하고 약 60초 동안 점진적으로 하락하는 대응 시나리오입니다.",
        },
        "eventResponse": {
            "riskSymbol": "AMD", "beneficiarySymbol": "OKE",
            "summary": "AMD 손절 기준과 알림을 관리하고 OKE 매수 타점을 제안합니다.",
        },
        "breakingNews": {
            "id": "simulated-geopolitical-risk",
            "headline": "중동 지정학적 긴장 고조…반도체 약세·에너지 강세",
            "summary": "중동 지역의 지정학적 긴장이 고조되면서 반도체 업종에 매도 압력이 확대되고 에너지 관련주는 강세를 보이고 있습니다.",
            "source": "GOPS Market Wire",
            "symbols": list(SYMBOLS),
        },
    }
    (output / "scenario.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "events.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )
    print(f"[OK] wrote {len(rows)} events to {output}")


def build_rows() -> list[dict[str, object]]:
    started_at = datetime(2026, 7, 10, 13, 30, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    trade_id = 10_000
    for second in range(300):
        timestamp = (started_at + timedelta(seconds=second)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for symbol in SYMBOLS:
            price = scenario_price(symbol, second)
            spread = {"AMD": 0.10, "OKE": 0.06}[symbol]
            bid = round(price - spread / 2, 4)
            ask = round(price + spread / 2, 4)
            size = 20 + ((second * 7 + len(symbol) * 11) % 180)
            common = {"atSeconds": float(second), "sourceTimestamp": timestamp}
            rows.append({
                **common,
                "payload": {"T": "t", "S": symbol, "i": trade_id, "x": "V", "p": price, "s": size, "c": [], "t": timestamp, "z": "C"},
            })
            rows.append({
                **common,
                "payload": {"T": "q", "S": symbol, "bx": "V", "bp": bid, "bs": size + 8, "ax": "V", "ap": ask, "as": size + 13, "c": [], "t": timestamp, "z": "C"},
            })
            trade_id += 1
    return rows


def scenario_price(symbol: str, second: int) -> float:
    if symbol == "AMD":
        if second < 170:
            value = 565 + 1.2 * math.sin(second / 10) + second * 0.006
        elif second < 210:
            value = 566.4 + 0.45 * math.sin(second / 5)
        else:
            elapsed = second - 210
            if elapsed < 5:
                value = 566.2
            else:
                movement = min(elapsed - 4, 60)
                progress = movement / 60
                eased = progress ** 1.4
                value = 566.2 + (525.5 - 566.2) * eased
    else:
        if second < 210:
            value = 90 + 0.28 * math.sin(second / 9) + second * 0.001
        else:
            elapsed = second - 210
            if elapsed < 5:
                value = 90.2
            else:
                movement = min(elapsed - 4, 85)
                progress = movement / 85
                eased = progress ** 1.2
                value = 90.2 + (95.5 - 90.2) * eased
    return round(value, 4)


if __name__ == "__main__":
    main()
