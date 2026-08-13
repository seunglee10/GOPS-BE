# 역할: Alpaca에 보낼 구독 요청 JSON을 미리 출력합니다.
# 사용: 실제 WebSocket 연결 전 종목/채널 신청 내용을 확인합니다.
# 실행: PYTHONPATH=systems/market-data/shared python scripts/local/preview-subscription.py 애플
import argparse
import sys

from market_data.alpaca.subscription import print_request


def main():
    parser = argparse.ArgumentParser(description="Alpaca 구독 요청 JSON을 출력합니다.")
    parser.add_argument("company_or_symbol", nargs="?", help="회사명 또는 심볼입니다. 예: 애플, AAPL")
    args = parser.parse_args()
    try:
        print_request(args.company_or_symbol)
    except ValueError as error:
        print(f"설정 오류: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
