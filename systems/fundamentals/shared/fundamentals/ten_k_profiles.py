from __future__ import annotations

import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable

from .backfill import (
    CompanyTicker,
    bool_env,
    build_redis_client,
    build_s3_client,
    load_universe_symbols,
    parse_company_tickers_exchange,
    parse_csv,
    redis_write_json,
    resolve_companies,
    unique_symbols,
    utc_iso,
)
from .sec_client import SecClient, SecRateLimiter


DEFAULT_UNIVERSE_PATH = "systems/market-data/config/sp500-universe.json"
DEFAULT_S3_PREFIX = "fundamentals/sec/10k-profiles"
RISK_CATEGORIES = (
    "공급망",
    "고객집중",
    "경쟁",
    "기술변화",
    "규제·법률",
    "지정학",
    "거시경제",
)
SEVERITY_HINTS = ("high", "medium", "low")
MAX_SECTION_CHARS_FOR_PROMPT = 120_000
MAX_REDIS_CARD_BYTES = 12_000


@dataclass(frozen=True)
class TenKFiling:
    cik: str
    accession: str
    primary_document: str
    filing_date: str
    report_date: str
    form: str = "10-K"


@dataclass(frozen=True)
class TenKSections:
    item_1_business: str
    item_1a_risk_factors: str


@dataclass
class TenKProfileBackfillConfig:
    dry_run: bool = True
    download_in_dry_run: bool = False
    force: bool = False
    universe_path: str = DEFAULT_UNIVERSE_PATH
    symbols: list[str] = field(default_factory=list)
    max_companies: int = 0
    s3_bucket: str = ""
    s3_prefix: str = DEFAULT_S3_PREFIX
    redis_ttl_seconds: int = 0
    user_agent: str = ""

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "TenKProfileBackfillConfig":
        environ = environ or os.environ
        return cls(
            dry_run=bool_env(environ.get("TEN_K_PROFILE_DRY_RUN"), True),
            download_in_dry_run=bool_env(environ.get("TEN_K_PROFILE_DOWNLOAD_IN_DRY_RUN"), False),
            force=bool_env(environ.get("TEN_K_PROFILE_FORCE"), False),
            universe_path=environ.get("SEC_UNIVERSE_PATH") or environ.get("ALPACA_UNIVERSE_REGISTRY_PATH") or DEFAULT_UNIVERSE_PATH,
            symbols=parse_csv(environ.get("TEN_K_PROFILE_SYMBOLS") or ""),
            max_companies=max(0, int(environ.get("TEN_K_PROFILE_MAX_COMPANIES") or "0")),
            s3_bucket=environ.get("S3_BUCKET") or "",
            s3_prefix=environ.get("TEN_K_PROFILE_S3_PREFIX") or DEFAULT_S3_PREFIX,
            redis_ttl_seconds=max(0, int(environ.get("TEN_K_PROFILE_REDIS_TTL_SECONDS") or "0")),
            user_agent=environ.get("SEC_USER_AGENT") or "",
        )


@dataclass
class TenKProfileStats:
    dry_run: bool
    companies_requested: int = 0
    companies_matched: int = 0
    filings_found: int = 0
    profiles_written: int = 0
    unchanged_skipped: int = 0
    failed: int = 0
    unmatched_symbols: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dryRun": self.dry_run,
            "companiesRequested": self.companies_requested,
            "companiesMatched": self.companies_matched,
            "filingsFound": self.filings_found,
            "profilesWritten": self.profiles_written,
            "unchangedSkipped": self.unchanged_skipped,
            "failed": self.failed,
            "unmatchedSymbols": list(self.unmatched_symbols),
        }


class _VisibleTextParser(HTMLParser):
    BLOCK_TAGS = {
        "address", "article", "br", "div", "dl", "dt", "dd", "h1", "h2", "h3", "h4",
        "h5", "h6", "li", "ol", "p", "section", "table", "tbody", "td", "th", "tr", "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        if lowered in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        if lowered in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


ITEM_1_PATTERN = re.compile(
    r"(?im)(?:^|\n)\s*item\s+1(?![a-z0-9])\s*[.:\-—]?\s*(?:business(?:\s+overview)?\b)?"
)
ITEM_1A_PATTERN = re.compile(
    r"(?im)(?:^|\n)\s*item\s+1a(?![a-z0-9])\s*[.:\-—]?\s*(?:risk\s+factors?\b)?"
)
RISK_END_PATTERN = re.compile(
    r"(?im)(?:^|\n)\s*item\s+(?:1b|1c|2)(?![a-z0-9])\s*[.:\-—]?"
)


def ten_k_profile_key(symbol: str) -> str:
    return f"profile:10k:{str(symbol or '').strip().upper()}"


def latest_ten_k_filing(submissions: dict[str, Any], cik: str) -> TenKFiling | None:
    recent = submissions.get("filings", {}).get("recent", {})
    if not isinstance(recent, dict):
        return None
    forms = list(recent.get("form") or [])
    accessions = list(recent.get("accessionNumber") or [])
    primary_documents = list(recent.get("primaryDocument") or [])
    filing_dates = list(recent.get("filingDate") or [])
    report_dates = list(recent.get("reportDate") or [])
    for index, form in enumerate(forms):
        if str(form or "").strip().upper() != "10-K":
            continue
        accession = value_at(accessions, index)
        primary_document = value_at(primary_documents, index)
        if not accession or not primary_document:
            continue
        return TenKFiling(
            cik=str(cik),
            accession=accession,
            primary_document=primary_document,
            filing_date=value_at(filing_dates, index),
            report_date=value_at(report_dates, index),
        )
    return None


def extract_10k_sections(document_html: str) -> TenKSections:
    parser = _VisibleTextParser()
    parser.feed(str(document_html or ""))
    parser.close()
    text = normalize_document_text("".join(parser.parts))
    if len(text) < 1000:
        raise ValueError("10-K document did not contain enough visible text.")

    item_1_matches = list(ITEM_1_PATTERN.finditer(text))
    item_1a_matches = list(ITEM_1A_PATTERN.finditer(text))
    business = select_section(text, item_1_matches, item_1a_matches, minimum_chars=1200)
    risk = select_section(text, item_1a_matches, list(RISK_END_PATTERN.finditer(text)), minimum_chars=1500)
    if not business:
        raise ValueError("10-K Item 1 business section could not be extracted.")
    if not risk:
        raise ValueError("10-K Item 1A risk factors section could not be extracted.")
    return TenKSections(item_1_business=business, item_1a_risk_factors=risk)


def select_section(
    text: str,
    starts: list[re.Match[str]],
    ends: list[re.Match[str]],
    *,
    minimum_chars: int,
) -> str:
    candidates: list[tuple[int, int, str]] = []
    for start in starts:
        for end in ends:
            if end.start() <= start.end():
                continue
            length = end.start() - start.end()
            if length < minimum_chars or length > 250_000:
                continue
            section = clean_section_text(text[start.end() : end.start()])
            if len(section) >= minimum_chars:
                candidates.append((start.start(), len(section), section))
            break
    if not candidates:
        return ""
    # The table of contents usually appears first. Prefer the latest plausible
    # heading, while keeping the extracted section large enough to be real body text.
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def normalize_document_text(value: str) -> str:
    value = html.unescape(value).replace("\xa0", " ").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def clean_section_text(value: str) -> str:
    return normalize_document_text(value).strip(" .:\n\t")


def business_model_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["structure", "segments", "revenueModel", "platform"],
        "properties": {
            "structure": {"type": "string", "minLength": 1, "maxLength": 160},
            "segments": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "detail"],
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 40},
                        "detail": {"type": "string", "minLength": 1, "maxLength": 160},
                    },
                },
            },
            "revenueModel": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "platform": {"type": ["string", "null"], "maxLength": 200},
        },
    }


def ten_k_profile_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["businessModel", "revenueDrivers", "competitivePosition", "riskFactors"],
        "properties": {
            "businessModel": business_model_json_schema(),
            "revenueDrivers": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {"type": "string", "minLength": 1, "maxLength": 320},
            },
            "competitivePosition": {"type": "string", "minLength": 1, "maxLength": 1200},
            "riskFactors": {
                "type": "array",
                "minItems": 1,
                "maxItems": 7,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["category", "summary", "severityHint"],
                    "properties": {
                        "category": {"type": "string", "enum": list(RISK_CATEGORIES)},
                        "summary": {"type": "string", "minLength": 1, "maxLength": 520},
                        "severityHint": {"type": "string", "enum": list(SEVERITY_HINTS)},
                    },
                },
            },
        },
    }


class TenKProfileSummarizer:
    def __init__(
        self,
        *,
        read_config: Callable[[str], str | None] | None = None,
        response_requester: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.read_config = read_config or os.getenv
        self.response_requester = response_requester or self._request_openai

    def summarize(
        self,
        *,
        company: CompanyTicker,
        filing: TenKFiling,
        sections: TenKSections,
        source_url: str,
        raw_sections_s3_key: str,
    ) -> dict[str, Any]:
        if not self.read_config("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required for 10-K profile generation.")
        request_payload = {
            "model": self.read_config("TEN_K_PROFILE_MODEL") or self.read_config("OPENAI_MODEL") or "gpt-5.2",
            "input": [
                {
                    "role": "system",
                    "content": (
                        "당신은 SEC 10-K 문서 전용 구조화 요약기입니다. 제공된 Item 1과 Item 1A에 직접 적힌 내용만 사용하세요. "
                        "회사 관점의 홍보성 표현과 과장 표현을 제거하고 중립적인 한국어로 요약하세요. 문서에 없는 제품, 수치, 전망, "
                        "원인 또는 투자 판단을 만들지 마세요. riskFactors는 제공된 고정 카테고리만 사용하고 같은 카테고리는 한 번만 쓰세요. "
                        "severityHint는 문서에서 강조된 노출 강도를 분류하는 보조값일 뿐 투자 위험 점수가 아닙니다. "
                        "businessModel은 완결 문장이 아니라 '항목 — 설명' 형태의 명사구로 작성하고 종결어미(합니다·입니다)를 쓰지 마세요. "
                        "structure는 설계·생산 구조 한 줄(예: '팹리스 — 설계 전담, 생산 외주'), segments는 보고 부문별 이름과 핵심 제품, "
                        "revenueModel은 수익 창출 방식 목록(판매 방식·라이선스·판매 채널), platform은 소프트웨어·개발 플랫폼이며 "
                        "문서에 해당 내용이 없으면 platform은 null로 두세요."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "symbol": company.symbol,
                            "companyName": company.company_name,
                            "filing": {
                                "form": filing.form,
                                "accession": filing.accession,
                                "filingDate": filing.filing_date,
                                "reportDate": filing.report_date,
                            },
                            "allowedRiskCategories": list(RISK_CATEGORIES),
                            "item1Business": sections.item_1_business[:MAX_SECTION_CHARS_FOR_PROMPT],
                            "item1aRiskFactors": sections.item_1a_risk_factors[:MAX_SECTION_CHARS_FOR_PROMPT],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ten_k_company_profile",
                    "strict": True,
                    "schema": ten_k_profile_json_schema(),
                }
            },
            "max_output_tokens": 2500,
        }
        parsed = self.response_requester(request_payload)
        content = validate_generated_profile(parsed)
        card = {
            "symbol": company.symbol,
            "companyName": company.company_name or None,
            "sourceFiling": f"10-K {filing.report_date[:4] or filing.filing_date[:4]} accession {filing.accession}",
            "sourceAccession": filing.accession,
            "sourceUrl": source_url,
            "filingDate": filing.filing_date,
            "reportDate": filing.report_date,
            "generatedAt": utc_iso(),
            "businessModel": content["businessModel"],
            "revenueDrivers": content["revenueDrivers"],
            "competitivePosition": content["competitivePosition"],
            "riskFactors": content["riskFactors"],
            "rawSectionsS3Key": raw_sections_s3_key,
        }
        encoded = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_REDIS_CARD_BYTES:
            raise ValueError(f"10-K profile card exceeds Redis size boundary: {len(encoded)} bytes")
        return card

    def _request_openai(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self.read_config("OPENAI_API_KEY")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        timeout = positive_float(self.read_config("TEN_K_PROFILE_TIMEOUT_SECONDS"), 90.0)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpenAI 10-K profile request failed with HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenAI 10-K profile request failed: {exc.__class__.__name__}") from exc
        return parse_openai_response_json(response_data)


def validate_business_model(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"structure", "segments", "revenueModel", "platform"}:
        raise ValueError("10-K business model output must match the structured contract.")
    structure = clean_bounded_text(payload.get("structure"), 160)
    segments: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for raw in payload.get("segments") or []:
        if not isinstance(raw, dict) or set(raw) != {"name", "detail"}:
            raise ValueError("10-K business segment output is invalid.")
        name = clean_bounded_text(raw.get("name"), 40)
        detail = clean_bounded_text(raw.get("detail"), 160)
        if not name or not detail:
            raise ValueError("10-K business segment output is missing content.")
        if name in seen_names:
            continue
        seen_names.add(name)
        segments.append({"name": name, "detail": detail})
    revenue_model = unique_text_list(payload.get("revenueModel"), limit=4, max_length=200)
    platform_raw = payload.get("platform")
    platform = clean_bounded_text(platform_raw, 200) if isinstance(platform_raw, str) else ""
    if not structure or not segments or not revenue_model:
        raise ValueError("10-K business model output is missing required content.")
    return {
        "structure": structure,
        "segments": segments,
        "revenueModel": revenue_model,
        "platform": platform or None,
    }


def validate_generated_profile(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("10-K profile output must be an object.")
    required = {"businessModel", "revenueDrivers", "competitivePosition", "riskFactors"}
    if set(payload) != required:
        raise ValueError("10-K profile output fields do not match the strict contract.")
    business_model = validate_business_model(payload.get("businessModel"))
    competitive_position = clean_bounded_text(payload.get("competitivePosition"), 1200)
    revenue_drivers = unique_text_list(payload.get("revenueDrivers"), limit=6, max_length=320)
    risk_factors: list[dict[str, str]] = []
    seen_categories: set[str] = set()
    for raw in payload.get("riskFactors") or []:
        if not isinstance(raw, dict) or set(raw) != {"category", "summary", "severityHint"}:
            raise ValueError("10-K risk factor output is invalid.")
        category = str(raw.get("category") or "").strip()
        severity = str(raw.get("severityHint") or "").strip().lower()
        summary = clean_bounded_text(raw.get("summary"), 520)
        if category not in RISK_CATEGORIES or severity not in SEVERITY_HINTS:
            raise ValueError("10-K risk factor enum is invalid.")
        if category in seen_categories:
            continue
        seen_categories.add(category)
        risk_factors.append({"category": category, "summary": summary, "severityHint": severity})
    if not business_model or not competitive_position or not revenue_drivers or not risk_factors:
        raise ValueError("10-K profile output is missing required content.")
    return {
        "businessModel": business_model,
        "revenueDrivers": revenue_drivers,
        "competitivePosition": competitive_position,
        "riskFactors": risk_factors,
    }


def run_ten_k_profile_backfill(
    config: TenKProfileBackfillConfig | None = None,
    *,
    sec_client: SecClient | None = None,
    redis_client: Any = None,
    s3_client: Any = None,
    summarizer: TenKProfileSummarizer | None = None,
) -> TenKProfileStats:
    config = config or TenKProfileBackfillConfig.from_env()
    validate_backfill_config(config)
    symbols = unique_symbols(config.symbols or load_universe_symbols(config.universe_path))
    if config.max_companies:
        symbols = symbols[: config.max_companies]
    stats = TenKProfileStats(dry_run=config.dry_run, companies_requested=len(symbols))

    if config.dry_run and not config.download_in_dry_run:
        print(json.dumps({
            "status": "dry-run",
            "message": "10-K profile dry-run skipped SEC and OpenAI calls.",
            "symbols": symbols[:10],
            "symbolCount": len(symbols),
        }, ensure_ascii=False), flush=True)
        return stats

    sec_client = sec_client or SecClient(
        user_agent=config.user_agent,
        rate_limiter=SecRateLimiter(max_requests_per_second=8),
    )
    ticker_map = parse_company_tickers_exchange(sec_client.company_tickers_exchange())
    company_map, unmatched = resolve_companies(symbols, ticker_map)
    stats.companies_matched = len(company_map)
    stats.unmatched_symbols = unmatched
    redis_client = redis_client if redis_client is not None else build_redis_client()
    s3_client = s3_client if s3_client is not None else build_s3_client()
    summarizer = summarizer or TenKProfileSummarizer()

    for symbol in symbols:
        company = company_map.get(symbol)
        if company is None:
            continue
        try:
            submissions = sec_client.submissions(company.cik)
            filing = latest_ten_k_filing(submissions, company.cik)
            if filing is None:
                raise ValueError("latest 10-K filing was not found")
            stats.filings_found += 1
            if not config.force and profile_accession(read_profile(redis_client, company.symbol)) == filing.accession:
                stats.unchanged_skipped += 1
                continue
            source_url = sec_client.filing_document_url(company.cik, filing.accession, filing.primary_document)
            document = sec_client.filing_document(company.cik, filing.accession, filing.primary_document)
            sections = extract_10k_sections(document)
            raw_key = upload_sections_to_s3(s3_client, config, company, filing, source_url, sections)
            if config.dry_run:
                print(json.dumps({
                    "status": "dry-run-profile-ready",
                    "symbol": company.symbol,
                    "accession": filing.accession,
                    "businessChars": len(sections.item_1_business),
                    "riskChars": len(sections.item_1a_risk_factors),
                    "rawSectionsS3Key": raw_key,
                }, ensure_ascii=False), flush=True)
                continue
            card = summarizer.summarize(
                company=company,
                filing=filing,
                sections=sections,
                source_url=source_url,
                raw_sections_s3_key=raw_key,
            )
            redis_write_json(
                redis_client,
                ten_k_profile_key(company.symbol),
                card,
                ttl_seconds=config.redis_ttl_seconds,
            )
            stats.profiles_written += 1
        except Exception as exc:
            stats.failed += 1
            print(json.dumps({
                "status": "warning",
                "symbol": symbol,
                "message": "10-K profile generation failed.",
                "error": exc.__class__.__name__,
            }, ensure_ascii=False), flush=True)

    print(json.dumps({"status": "success", **stats.to_dict()}, ensure_ascii=False), flush=True)
    return stats


def upload_sections_to_s3(
    s3_client: Any,
    config: TenKProfileBackfillConfig,
    company: CompanyTicker,
    filing: TenKFiling,
    source_url: str,
    sections: TenKSections,
) -> str:
    key = (
        f"{config.s3_prefix.strip('/')}/{company.symbol}/"
        f"{filing.accession.replace('-', '')}/sections.json"
    )
    payload = {
        "symbol": company.symbol,
        "companyName": company.company_name,
        "cik": company.cik,
        "sourceUrl": source_url,
        "accession": filing.accession,
        "filingDate": filing.filing_date,
        "reportDate": filing.report_date,
        "extractedAt": utc_iso(),
        "item1Business": sections.item_1_business,
        "item1aRiskFactors": sections.item_1a_risk_factors,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not config.dry_run:
        s3_client.put_object(
            Bucket=config.s3_bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            Metadata={"sha256": hashlib.sha256(body).hexdigest(), "source": "sec-10k-sections"},
        )
    return key


def validate_backfill_config(config: TenKProfileBackfillConfig) -> None:
    network_enabled = not config.dry_run or config.download_in_dry_run
    if network_enabled and (not config.user_agent or "YOUR_" in config.user_agent):
        raise SystemExit("SEC_USER_AGENT must include real contact information for 10-K collection.")
    if not config.dry_run:
        if not config.s3_bucket:
            raise SystemExit("S3_BUCKET is required for 10-K profile generation.")
        if not os.getenv("REDIS_URL"):
            raise SystemExit("REDIS_URL is required for 10-K profile generation.")
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is required for 10-K profile generation.")


def read_profile(redis_client: Any, symbol: str) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    try:
        value = redis_client.get(ten_k_profile_key(symbol))
    except Exception:
        return None
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def profile_accession(profile: dict[str, Any] | None) -> str:
    if not isinstance(profile, dict):
        return ""
    return str(profile.get("sourceAccession") or "").strip()


def parse_openai_response_json(response_data: dict[str, Any]) -> dict[str, Any]:
    output_text = response_data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return json.loads(output_text)
    for output in response_data.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return json.loads(text)
    raise ValueError("OpenAI response did not contain structured JSON output.")


def value_at(values: list[Any], index: int) -> str:
    return str(values[index] or "").strip() if index < len(values) else ""


def clean_bounded_text(value: Any, maximum: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def unique_text_list(value: Any, *, limit: int, max_length: int) -> list[str]:
    result: list[str] = []
    for raw in value or []:
        text = clean_bounded_text(raw, max_length)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
