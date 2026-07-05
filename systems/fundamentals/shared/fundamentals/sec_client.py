from __future__ import annotations

import gzip
import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


SEC_DATA_BASE_URL = "https://data.sec.gov"
SEC_ARCHIVE_BASE_URL = "https://www.sec.gov/Archives/edgar/daily-index"


@dataclass
class SecRateLimiter:
    max_requests_per_second: float = 8.0
    _last_request_at: float = field(default=0.0, init=False)

    def wait(self) -> None:
        if self.max_requests_per_second <= 0:
            return
        minimum_interval = 1.0 / float(self.max_requests_per_second)
        now = time.monotonic()
        sleep_for = minimum_interval - (now - self._last_request_at)
        if sleep_for > 0:
            time.sleep(sleep_for)
        self._last_request_at = time.monotonic()


class SecClient:
    def __init__(self, *, user_agent: str | None = None, rate_limiter: SecRateLimiter | None = None):
        resolved_user_agent = user_agent or os.getenv("SEC_USER_AGENT")
        if not resolved_user_agent:
            raise ValueError("SEC_USER_AGENT is required and must include contact information.")
        self.user_agent = resolved_user_agent
        self.rate_limiter = rate_limiter or SecRateLimiter()

    def companyfacts(self, cik: str) -> dict[str, Any]:
        return self.get_json(f"{SEC_DATA_BASE_URL}/api/xbrl/companyfacts/CIK{normalize_cik(cik)}.json")

    def submissions(self, cik: str) -> dict[str, Any]:
        return self.get_json(f"{SEC_DATA_BASE_URL}/submissions/CIK{normalize_cik(cik)}.json")

    def frame(self, taxonomy: str, concept: str, unit: str, frame_period: str) -> dict[str, Any]:
        taxonomy_part = urllib.parse.quote(str(taxonomy or "us-gaap"), safe="")
        concept_part = urllib.parse.quote(str(concept or ""), safe="")
        unit_part = urllib.parse.quote(str(unit or "USD"), safe="")
        frame_part = urllib.parse.quote(str(frame_period or ""), safe="")
        return self.get_json(f"{SEC_DATA_BASE_URL}/api/xbrl/frames/{taxonomy_part}/{concept_part}/{unit_part}/{frame_part}.json")

    def company_tickers_exchange(self) -> dict[str, Any]:
        return self.get_json("https://www.sec.gov/files/company_tickers_exchange.json")

    def get_json(self, url: str) -> dict[str, Any]:
        self.rate_limiter.wait()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
        )
        with urllib.request.urlopen(request, timeout=float(os.getenv("SEC_HTTP_TIMEOUT_SECONDS", "20"))) as response:
            raw = response.read()
            encoding = str(response.headers.get("Content-Encoding") or "").lower()
        if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def normalize_cik(cik: str) -> str:
    digits = "".join(ch for ch in str(cik or "") if ch.isdigit())
    if not digits:
        raise ValueError("CIK must contain digits.")
    return digits.zfill(10)
