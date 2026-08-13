# 역할: Kafka Raw Alpaca 데이터를 S3 raw archive에 저장합니다.
# 사용: raw Kafka retention 밖에서도 replay/repair 증거를 남깁니다.
from market_data.storage.raw_s3_archive_sink import main


if __name__ == "__main__":
    main()
