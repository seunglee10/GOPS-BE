from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from gops_simul.config import PROJECT_ROOT


SCENARIO_ID = "saturday-demo-amd-iff-oke"
SYMBOLS = ("AMD", "IFF", "OKE")
SEED_PRICES = {"AMD": 200.0, "IFF": 75.0, "OKE": 82.0}
PHASES = (
    ("market-overview", "시장 조망", 0, "시장 트리맵과 오늘의 흐름을 확인합니다."),
    ("recommendation", "추천 종목", 30, "투자 성향과 포트폴리오를 반영한 추천 종목을 확인합니다."),
    ("company-research", "기업 분석", 60, "기업 정보·뉴스·온톨로지 관계를 확인합니다."),
    ("chart-analysis", "차트 분석", 90, "지지선·저항선과 삼각 수렴 패턴을 확인합니다."),
    ("order-ready", "예약매매 설정", 130, "추천 타점에 예약매매와 알림을 설정합니다."),
    ("market-open", "본장 시작", 170, "관심종목의 체결·호가·풋프린트를 동시에 관찰합니다."),
    ("breaking-event", "지정학 이벤트", 210, "반도체 약세와 에너지 강세에 대응합니다."),
    ("market-close", "장 마감·복기", 285, "주문·알림 상태와 오늘의 판단을 리포트로 복기합니다."),
)


def main() -> None:
    output = PROJECT_ROOT / "data" / "scenarios" / SCENARIO_ID
    output.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    manifest = {
        "scenarioId": SCENARIO_ID,
        "title": "Saturday operator demo · AMD, IFF, OKE",
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
                "symbol": "IFF", "rank": 1, "score": 92, "confidence": 0.86,
                "sector": "Materials", "changePercent": 1.4,
                "reasons": ["삼각 수렴 상단 접근", "포트폴리오 업종 분산"],
                "riskWarnings": ["이벤트 발생 시 변동성 확대 가능"],
            },
            {
                "symbol": "AMD", "rank": 2, "score": 84, "confidence": 0.79,
                "sector": "Information Technology", "changePercent": 0.6,
                "reasons": ["거래량 회복", "지지 구간 재확인"],
                "riskWarnings": ["반도체 집중 위험"],
            },
            {
                "symbol": "OKE", "rank": 3, "score": 78, "confidence": 0.74,
                "sector": "Energy", "changePercent": 0.2,
                "reasons": ["에너지 업종 분산", "이벤트 헤지 후보"],
                "riskWarnings": ["뉴스 민감도 높음"],
            },
        ],
        "chartAnalysis": {
            "symbol": "IFF", "pattern": "삼각 수렴 패턴", "support": 74.80,
            "resistance": 76.20, "entry": 76.30, "stop": 74.60,
            "summary": "고점과 저점 간격이 좁아진 뒤 76.20 저항 돌파를 확인하는 시나리오입니다.",
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
            spread = 0.04 if symbol == "AMD" else 0.02
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
            value = 200 + 0.55 * math.sin(second / 10) + second * 0.003
        elif second < 210:
            value = 200.65 + 0.22 * math.sin(second / 5)
        else:
            progress = (second - 210) / 89
            value = 200.5 + (185.0 - 200.5) * progress + 0.16 * math.sin(second / 3)
    elif symbol == "IFF":
        if second < 90:
            amplitude = max(0.12, 1.45 * (1 - second / 100))
            value = 75 + amplitude * math.sin(second / 4.5)
        elif second < 130:
            progress = (second - 90) / 40
            value = 75.2 + 3.1 * progress + 0.12 * math.sin(second / 3)
        elif second < 210:
            value = 78.25 + 0.18 * math.sin(second / 7)
        else:
            progress = (second - 210) / 89
            value = 78.2 + (76.5 - 78.2) * progress + 0.08 * math.sin(second / 4)
    else:
        if second < 210:
            value = 82 + 0.24 * math.sin(second / 9) + second * 0.001
        else:
            progress = (second - 210) / 89
            value = 82.15 + (87.2 - 82.15) * progress + 0.09 * math.sin(second / 3.5)
    return round(value, 4)


if __name__ == "__main__":
    main()
