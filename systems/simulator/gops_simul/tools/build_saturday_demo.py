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
            "summary": "알림 후 5초간 가격을 유지하고 완만히 하락한 뒤 중간 구간에서 낙폭이 확대되는 대응 시나리오입니다.",
        },
        "eventResponse": {
            "riskSymbol": "AMD", "beneficiarySymbol": "OKE",
            "summary": "AMD 손절 기준과 알림을 관리하고 OKE 매수 타점을 제안합니다.",
        },
        "breakingNews": {
            "id": "simulated-geopolitical-risk",
            "headline": "[시뮬레이션 속보] 지정학적 긴장 고조…반도체 약세·에너지 강세",
            "summary": "시연 시나리오가 지정학 이벤트를 발생시켰습니다. 실제 뉴스가 아니며 AMD 하락·OKE 상승 체결과 호가가 이어집니다.",
            "source": "GOPS 시뮬레이터",
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
            elif elapsed < 30:
                movement = elapsed - 4
                progress = movement / 25
                eased = progress * progress * (3 - 2 * progress)
                noise = 0.02 * math.sin(second / 3) * math.sin(math.pi * progress)
                value = 566.2 + (564.2 - 566.2) * eased + noise
            elif elapsed < 75:
                movement = elapsed - 4
                progress = (movement - 25) / 45
                eased = progress * progress * (3 - 2 * progress)
                noise = 0.03 * math.sin(second / 3) * math.sin(math.pi * progress)
                value = 564.2 + (532.0 - 564.2) * eased + noise
            else:
                movement = elapsed - 4
                progress = (movement - 70) / 15
                eased = progress * progress * (3 - 2 * progress)
                noise = 0.02 * math.sin(second / 3) * math.sin(math.pi * progress)
                value = 532.0 + (525.5 - 532.0) * eased + noise
    else:
        if second < 210:
            value = 90 + 0.28 * math.sin(second / 9) + second * 0.001
        else:
            progress = (second - 210) / 89
            eased = progress * progress * (3 - 2 * progress)
            noise = 0.06 * math.sin(second / 3.5) * math.sin(math.pi * progress)
            value = 90.2 + (95.5 - 90.2) * eased + noise
    return round(value, 4)


if __name__ == "__main__":
    main()
