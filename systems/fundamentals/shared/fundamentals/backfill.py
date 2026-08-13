from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .concepts import CONCEPT_MAP, parse_concept_ref
from .metrics import calculate_derived_metrics, q4_synthetic_fact, usable_fact
from .redis_keys import fundamentals_peer_latest_key, fundamentals_peer_key, fundamentals_summary_key
from .schema import CLICKHOUSE_COMPATIBILITY_MIGRATIONS, CLICKHOUSE_TABLES
from .sec_client import SecClient, SecRateLimiter, normalize_cik


SEC_COMPANYFACTS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
DEFAULT_COMPANYFACTS_S3_PREFIX = "fundamentals/sec/companyfacts"
DEFAULT_COMPANYFACTS_SOURCE = "api"
DEFAULT_UNIVERSE_PATH = "systems/market-data/config/sp500-universe.json"
SKIP_RAW_METRIC_GROUPS = {"debt_current_components", "debt_noncurrent_components"}
FLOW_FACT_METRICS = {"revenue", "net_income", "operating_income", "operating_cash_flow", "capex", "interest_expense"}
PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}
DEFAULT_FRAME_PERIOD_COUNT = 6
FRAME_FILING_LAG_DAYS = 45
INSTANT_FRAME_CONCEPTS = {
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "EntityCommonStockSharesOutstanding",
}
SHARE_FRAME_CONCEPTS = {"EntityCommonStockSharesOutstanding"}
FRAME_CONCEPT_PRIORITY = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": 0,
    "RevenueFromContractWithCustomerIncludingAssessedTax": 1,
    "Revenues": 2,
    "SalesRevenueNet": 3,
    "OperatingIncomeLoss": 4,
    "NetIncomeLoss": 5,
    "Assets": 6,
    "Liabilities": 7,
    "StockholdersEquity": 8,
    "EntityCommonStockSharesOutstanding": 9,
}


@dataclass
class FundamentalsBackfillConfig:
    dry_run: bool = True
    source: str = DEFAULT_COMPANYFACTS_SOURCE
    companyfacts_zip_url: str = SEC_COMPANYFACTS_ZIP_URL
    local_zip_path: str = ""
    s3_zip_key: str = ""
    s3_bucket: str = ""
    s3_prefix: str = DEFAULT_COMPANYFACTS_S3_PREFIX
    universe_path: str = DEFAULT_UNIVERSE_PATH
    symbols: list[str] = field(default_factory=list)
    max_companies: int = 0
    batch_size: int = 500
    load_companyfacts: bool = True
    load_frames: bool = True
    write_frame_rows: bool = True
    frame_concepts: set[str] = field(default_factory=set)
    frame_periods: list[str] = field(default_factory=list)
    redis_ttl_seconds: int = 0
    download_in_dry_run: bool = False
    user_agent: str = ""

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "FundamentalsBackfillConfig":
        environ = environ or os.environ
        return cls(
            dry_run=bool_env(environ.get("SEC_FUNDAMENTALS_DRY_RUN"), True),
            source=(environ.get("SEC_FUNDAMENTALS_SOURCE") or DEFAULT_COMPANYFACTS_SOURCE).strip().lower(),
            companyfacts_zip_url=environ.get("SEC_COMPANYFACTS_ZIP_URL") or SEC_COMPANYFACTS_ZIP_URL,
            local_zip_path=environ.get("SEC_COMPANYFACTS_ZIP_PATH") or "",
            s3_zip_key=environ.get("SEC_COMPANYFACTS_S3_KEY") or "",
            s3_bucket=environ.get("S3_BUCKET") or "",
            s3_prefix=environ.get("SEC_FUNDAMENTALS_S3_PREFIX") or DEFAULT_COMPANYFACTS_S3_PREFIX,
            universe_path=environ.get("SEC_UNIVERSE_PATH") or environ.get("ALPACA_UNIVERSE_REGISTRY_PATH") or DEFAULT_UNIVERSE_PATH,
            symbols=parse_csv(environ.get("SEC_FUNDAMENTALS_SYMBOLS") or ""),
            max_companies=int(environ.get("SEC_FUNDAMENTALS_MAX_COMPANIES") or "0"),
            batch_size=max(1, int(environ.get("SEC_FUNDAMENTALS_BATCH_SIZE") or "500")),
            load_companyfacts=bool_env(environ.get("SEC_FUNDAMENTALS_LOAD_COMPANYFACTS"), True),
            load_frames=bool_env(environ.get("SEC_FUNDAMENTALS_LOAD_FRAMES"), True),
            write_frame_rows=bool_env(environ.get("SEC_FUNDAMENTALS_WRITE_FRAME_ROWS"), True),
            frame_concepts=set(parse_csv(environ.get("SEC_FUNDAMENTALS_FRAME_CONCEPTS") or default_frame_concepts_csv())),
            frame_periods=parse_csv(environ.get("SEC_FUNDAMENTALS_FRAME_PERIODS") or ""),
            redis_ttl_seconds=int(environ.get("SEC_FUNDAMENTALS_REDIS_TTL_SECONDS") or "0"),
            download_in_dry_run=bool_env(environ.get("SEC_FUNDAMENTALS_DOWNLOAD_IN_DRY_RUN"), False),
            user_agent=environ.get("SEC_USER_AGENT") or "",
        )


@dataclass
class CompanyTicker:
    symbol: str
    cik: str
    company_name: str = ""
    exchange: str = ""


@dataclass
class BackfillStats:
    run_id: str
    dry_run: bool
    companies_requested: int = 0
    companies_matched: int = 0
    companies_loaded: int = 0
    companies_failed: int = 0
    unmatched_symbols: list[str] = field(default_factory=list)
    fact_rows: int = 0
    derived_rows: int = 0
    frame_rows: int = 0
    redis_summaries: int = 0
    redis_peer_summaries: int = 0
    raw_s3_object: str = ""
    checksum_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "dryRun": self.dry_run,
            "companiesRequested": self.companies_requested,
            "companiesMatched": self.companies_matched,
            "companiesLoaded": self.companies_loaded,
            "companiesFailed": self.companies_failed,
            "unmatchedSymbols": list(self.unmatched_symbols),
            "factRows": self.fact_rows,
            "derivedRows": self.derived_rows,
            "frameRows": self.frame_rows,
            "redisSummaries": self.redis_summaries,
            "redisPeerSummaries": self.redis_peer_summaries,
            "rawS3Object": self.raw_s3_object,
            "checksumSha256": self.checksum_sha256,
        }


def run_companyfacts_backfill(config: FundamentalsBackfillConfig | None = None, *, clickhouse_client=None, redis_client=None, s3_client=None, sec_client=None) -> BackfillStats:
    config = config or FundamentalsBackfillConfig.from_env()
    started_at = utc_now()
    run_id = f"sec-companyfacts-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    stats = BackfillStats(run_id=run_id, dry_run=config.dry_run)

    if not config.dry_run:
        validate_write_config(config)

    symbols = config.symbols or load_universe_symbols(config.universe_path)
    symbols = unique_symbols(symbols)
    if config.max_companies > 0:
        symbols = symbols[: config.max_companies]
    stats.companies_requested = len(symbols)

    source = effective_source(config)

    if config.dry_run and not config.download_in_dry_run and not config.local_zip_path:
        print(json.dumps({
            "status": "dry-run",
            "message": "SEC fundamentals dry-run skipped network download. Set SEC_FUNDAMENTALS_DRY_RUN=false to ingest or SEC_FUNDAMENTALS_DOWNLOAD_IN_DRY_RUN=true to test download.",
            "symbols": symbols[:10],
            "symbolCount": len(symbols),
            "source": source,
            "companyfactsZipUrl": config.companyfacts_zip_url,
        }, ensure_ascii=False), flush=True)
        return stats

    sec_client = sec_client or SecClient(user_agent=config.user_agent, rate_limiter=SecRateLimiter(max_requests_per_second=8))
    ticker_payload = sec_client.company_tickers_exchange()
    ticker_map = parse_company_tickers_exchange(ticker_payload)
    company_map, unmatched = resolve_companies(symbols, ticker_map)
    stats.companies_matched = len(company_map)
    stats.unmatched_symbols = unmatched

    zip_path: Path | None = None
    if config.load_companyfacts and source == "zip":
        zip_path, checksum = resolve_companyfacts_zip(config, s3_client=s3_client)
        stats.checksum_sha256 = checksum
        if not config.dry_run:
            if config.s3_zip_key:
                stats.raw_s3_object = config.s3_zip_key
            else:
                stats.raw_s3_object = upload_companyfacts_zip_to_s3(config, zip_path, checksum, s3_client=s3_client)
    elif config.load_companyfacts and source == "api" and not config.dry_run:
        stats.raw_s3_object = f"{config.s3_prefix.strip('/')}/api/"

    if clickhouse_client is None:
        clickhouse_client = build_clickhouse_client()
    if redis_client is None:
        redis_client = build_redis_client()

    if not config.dry_run:
        ensure_sec_clickhouse_schema(clickhouse_client)
        insert_collection_run(clickhouse_client, stats, status="running", started_at=started_at, finished_at=None)
        insert_company_ticker_rows(clickhouse_client, company_map, symbols, ticker_payload, batch_size=config.batch_size)
        if config.load_companyfacts:
            insert_raw_artifact_row(clickhouse_client, config, stats, started_at, source=source)

    all_frame_rows: list[dict[str, Any]] = []
    if config.load_companyfacts:
        if source == "api":
            payload_iter = iter_companyfacts_api_payloads(sec_client, company_map, config=config, stats=stats, s3_client=s3_client)
        elif zip_path is not None:
            payload_iter = iter_companyfacts_payloads(zip_path, company_map)
        else:
            payload_iter = iter([])
        for company, payload in payload_iter:
            fact_rows = normalize_companyfacts_payload(company, payload)
            fact_rows = add_synthetic_q4_rows(fact_rows)
            derived_rows = derive_metric_rows(company, fact_rows)
            stats.companies_loaded += 1
            stats.fact_rows += len(fact_rows)
            stats.derived_rows += len(derived_rows)
            if not config.dry_run:
                insert_batches(clickhouse_client, "sec_financial_facts", fact_rows, config.batch_size)
                insert_batches(clickhouse_client, "sec_derived_metrics", derived_rows, config.batch_size)
                if write_redis_summary(redis_client, company.symbol, fact_rows, derived_rows, ttl_seconds=config.redis_ttl_seconds):
                    stats.redis_summaries += 1

    if config.load_frames:
        if zip_path is not None:
            for frame_rows in iter_frame_rows(zip_path, company_map, config.frame_concepts):
                if not frame_rows:
                    continue
                stats.frame_rows += len(frame_rows)
                all_frame_rows.extend(frame_rows)
                if not config.dry_run and config.write_frame_rows:
                    insert_batches(clickhouse_client, "sec_frames", frame_rows, config.batch_size)

        for frame_rows in fetch_sec_frame_api_rows(sec_client, company_map, config.frame_concepts, config.frame_periods):
            if not frame_rows:
                continue
            stats.frame_rows += len(frame_rows)
            all_frame_rows.extend(frame_rows)
            if not config.dry_run and config.write_frame_rows:
                insert_batches(clickhouse_client, "sec_frames", frame_rows, config.batch_size)

    if not config.dry_run and all_frame_rows:
        stats.redis_peer_summaries = write_redis_peer_summaries(redis_client, all_frame_rows, ttl_seconds=config.redis_ttl_seconds)

    if not config.dry_run:
        insert_collection_run(clickhouse_client, stats, status="success", started_at=started_at, finished_at=utc_now())

    print(json.dumps({"status": "success", **stats.to_dict()}, ensure_ascii=False, default=str), flush=True)
    return stats


def effective_source(config: FundamentalsBackfillConfig) -> str:
    if config.local_zip_path or config.s3_zip_key:
        return "zip"
    source = (config.source or DEFAULT_COMPANYFACTS_SOURCE).strip().lower()
    return source if source in {"api", "zip"} else DEFAULT_COMPANYFACTS_SOURCE


def iter_companyfacts_api_payloads(
    sec_client: SecClient,
    company_map: dict[str, CompanyTicker],
    *,
    config: FundamentalsBackfillConfig,
    stats: BackfillStats | None = None,
    s3_client=None,
) -> Iterable[tuple[CompanyTicker, dict[str, Any]]]:
    by_cik = companies_by_cik(company_map)
    client = None
    for cik in sorted(by_cik):
        try:
            payload = sec_client.companyfacts(cik)
        except Exception as exc:
            if stats is not None:
                stats.companies_failed += 1
            print(json.dumps({
                "status": "warning",
                "message": "SEC companyfacts API request failed.",
                "cik": cik,
                "symbols": [company.symbol for company in by_cik[cik]],
                "error": f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else exc.__class__.__name__,
            }, ensure_ascii=False), flush=True)
            continue
        if not isinstance(payload, dict) or "facts" not in payload:
            continue
        if not config.dry_run and config.s3_bucket:
            if client is None:
                client = s3_client or build_s3_client()
            upload_companyfacts_json_to_s3(client, config, cik, payload)
        for company in by_cik[cik]:
            yield company, payload


def upload_companyfacts_json_to_s3(s3_client: Any, config: FundamentalsBackfillConfig, cik: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = f"{config.s3_prefix.strip('/')}/api/CIK{cik}.json"
    s3_client.put_object(
        Bucket=config.s3_bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        Metadata={
            "sha256": hashlib.sha256(body).hexdigest(),
            "source": "sec-companyfacts-api",
        },
    )
    return key


def validate_write_config(config: FundamentalsBackfillConfig) -> None:
    if not config.user_agent or "YOUR_" in config.user_agent:
        raise SystemExit("SEC_USER_AGENT must be set to a real contact-bearing User-Agent before SEC fundamentals ingestion.")
    if config.load_companyfacts and not config.s3_bucket:
        raise SystemExit("S3_BUCKET is required for SEC fundamentals ingestion.")


def resolve_companyfacts_zip(config: FundamentalsBackfillConfig, *, s3_client=None) -> tuple[Path, str]:
    if config.local_zip_path:
        path = Path(config.local_zip_path)
        if not path.exists():
            raise FileNotFoundError(str(path))
        return path, sha256_file(path)
    if config.s3_zip_key:
        destination = Path(tempfile.gettempdir()) / f"companyfacts-s3-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.zip"
        download_companyfacts_zip_from_s3(config, destination, s3_client=s3_client)
        return destination, sha256_file(destination)
    destination = Path(tempfile.gettempdir()) / f"companyfacts-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.zip"
    download_file(config.companyfacts_zip_url, destination, user_agent=config.user_agent)
    return destination, sha256_file(destination)


def download_companyfacts_zip_from_s3(config: FundamentalsBackfillConfig, destination: Path, *, s3_client=None) -> None:
    if not config.s3_bucket:
        raise SystemExit("S3_BUCKET is required when SEC_COMPANYFACTS_S3_KEY is set.")
    client = s3_client or build_s3_client()
    client.download_file(config.s3_bucket, config.s3_zip_key, str(destination))


def download_file(url: str, destination: Path, *, user_agent: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent or "GOPS SEC fundamentals dry-run", "Accept": "application/zip"})
    with urllib.request.urlopen(request, timeout=float(os.getenv("SEC_HTTP_TIMEOUT_SECONDS", "120"))) as response:
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)


def upload_companyfacts_zip_to_s3(config: FundamentalsBackfillConfig, zip_path: Path, checksum: str, *, s3_client=None) -> str:
    client = s3_client or build_s3_client()
    run_date = os.getenv("SEC_FUNDAMENTALS_RUN_DATE") or utc_now().strftime("%Y-%m-%d")
    key = f"{config.s3_prefix.strip('/')}/{run_date}/companyfacts.zip"
    client.upload_file(
        str(zip_path),
        config.s3_bucket,
        key,
        ExtraArgs={
            "ContentType": "application/zip",
            "Metadata": {
                "sha256": checksum,
                "source": "sec-companyfacts-bulk",
            },
        },
    )
    return key


def iter_companyfacts_payloads(zip_path: Path, company_map: dict[str, CompanyTicker]) -> Iterable[tuple[CompanyTicker, dict[str, Any]]]:
    by_cik = companies_by_cik(company_map)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            cik = cik_from_zip_name(info.filename)
            if not cik or cik not in by_cik:
                continue
            with archive.open(info) as handle:
                payload = json.loads(handle.read().decode("utf-8"))
            if isinstance(payload, dict) and "facts" in payload:
                for company in by_cik[cik]:
                    yield company, payload


def iter_frame_rows(zip_path: Path, company_map: dict[str, CompanyTicker], frame_concepts: set[str]) -> Iterable[list[dict[str, Any]]]:
    by_cik = companies_by_cik(company_map)
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if not likely_frame_file(info.filename):
                continue
            with archive.open(info) as handle:
                try:
                    payload = json.loads(handle.read().decode("utf-8"))
                except Exception:
                    continue
            if not is_frame_payload(payload):
                continue
            rows = normalize_frame_payload(payload, info.filename, by_cik, frame_concepts)
            if rows:
                yield rows


def fetch_sec_frame_api_rows(
    sec_client: SecClient,
    company_map: dict[str, CompanyTicker],
    frame_concepts: set[str],
    frame_periods: list[str] | None = None,
) -> Iterable[list[dict[str, Any]]]:
    by_cik = companies_by_cik(company_map)
    periods = frame_periods or default_frame_periods()
    concept_refs = sorted(frame_concepts or set(parse_csv(default_frame_concepts_csv())), key=frame_concept_sort_key)
    for concept_ref in concept_refs:
        taxonomy, concept = parse_concept_ref(concept_ref)
        if not concept:
            continue
        unit = frame_unit_for_concept(concept)
        for period in frame_periods_for_concept(concept, periods):
            filename = f"api/{taxonomy}/{concept}/{unit}/{period}.json"
            try:
                payload = sec_client.frame(taxonomy, concept, unit, period)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    print(json.dumps({
                        "status": "warning",
                        "message": "SEC frame API request failed.",
                        "concept": concept,
                        "unit": unit,
                        "framePeriod": period,
                        "error": f"HTTP {exc.code}",
                    }, ensure_ascii=False), flush=True)
                continue
            except Exception as exc:
                print(json.dumps({
                    "status": "warning",
                    "message": "SEC frame API request failed.",
                    "concept": concept,
                    "unit": unit,
                    "framePeriod": period,
                    "error": exc.__class__.__name__,
                }, ensure_ascii=False), flush=True)
                continue
            if not is_frame_payload(payload):
                continue
            rows = normalize_frame_payload(payload, filename, by_cik, {concept})
            if rows:
                yield rows


def normalize_companyfacts_payload(company: CompanyTicker, payload: dict[str, Any]) -> list[dict[str, Any]]:
    taxonomy_facts = (payload.get("facts") or {}) if isinstance(payload, dict) else {}
    rows_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    raw_metric_concepts = {metric: concepts for metric, concepts in CONCEPT_MAP.items() if metric not in SKIP_RAW_METRIC_GROUPS}
    for metric, concept_refs in raw_metric_concepts.items():
        for priority, concept_ref in enumerate(concept_refs):
            taxonomy, concept = parse_concept_ref(concept_ref)
            concept_payload = (taxonomy_facts.get(taxonomy) or {}).get(concept) or {}
            units = concept_payload.get("units") or {}
            for unit, facts in units.items():
                for fact in facts or []:
                    if not usable_fact(fact):
                        continue
                    row = fact_row(company, metric, taxonomy, concept, priority, unit, fact)
                    if row is None:
                        continue
                    key = (
                        row["symbol"],
                        row["metric"],
                        row["unit"],
                        row["fiscal_year"],
                        row["fiscal_period"],
                        row["period_end"],
                    )
                    current = rows_by_key.get(key)
                    if current is None or row_sort_key(row) > row_sort_key(current):
                        rows_by_key[key] = row
    return sorted(rows_by_key.values(), key=lambda item: (item["symbol"], item["metric"], item["fiscal_year"], item["fiscal_period"], item["period_end"]))


def fact_row(company: CompanyTicker, metric: str, taxonomy: str, concept: str, priority: int, unit: str, fact: dict[str, Any]) -> dict[str, Any] | None:
    fiscal_year = int_or_none(fact.get("fy"))
    fiscal_period = str(fact.get("fp") or "").strip().upper()
    period_end = date_string(fact.get("end"))
    filed_at = date_string(fact.get("filed"))
    if fiscal_year is None or not fiscal_period or not period_end or not filed_at:
        return None
    if metric == "equity" and concept == "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest":
        quality = "equity_includes_nci"
    elif metric == "cash_and_cash_equivalents" and concept == "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents":
        quality = "cash_includes_restricted"
    else:
        quality = "available"
    accession = str(fact.get("accn") or "")
    return {
        "symbol": company.symbol,
        "cik": company.cik,
        "metric": metric,
        "taxonomy": taxonomy,
        "concept": concept,
        "unit": str(unit),
        "value": float_value(fact.get("val")),
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "period_end": period_end,
        "form": str(fact.get("form") or ""),
        "accession": accession,
        "filed_at": filed_at,
        "quality": quality,
        "raw": json.dumps({
            "selected_concept": concept,
            "taxonomy": taxonomy,
            "concept_priority": priority,
            "unit": unit,
            "accession": accession,
            "filed_at": filed_at,
            "quality": quality,
            "frame": fact.get("frame"),
        }, ensure_ascii=False, separators=(",", ":")),
        "version_filed_at": filed_at,
    }


def add_synthetic_q4_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = list(rows)
    by_key: dict[tuple[str, str, int, str], dict[str, dict[str, Any]]] = {}
    for row in rows:
        if row["metric"] not in FLOW_FACT_METRICS:
            continue
        key = (row["symbol"], row["metric"], int(row["fiscal_year"]), row["unit"])
        by_key.setdefault(key, {})[row["fiscal_period"]] = row
    for (_symbol, _metric, _fy, _unit), period_rows in by_key.items():
        if "Q4" in period_rows or not all(period in period_rows for period in ("FY", "Q1", "Q2", "Q3")):
            continue
        synthetic = q4_synthetic_fact(
            row_to_metric_fact(period_rows["FY"]),
            row_to_metric_fact(period_rows["Q1"]),
            row_to_metric_fact(period_rows["Q2"]),
            row_to_metric_fact(period_rows["Q3"]),
        )
        base = period_rows["FY"]
        raw = dict(synthetic.get("raw") or {})
        raw["selected_concept"] = base["concept"]
        result.append({
            **base,
            "value": float_value(synthetic.get("value")),
            "fiscal_period": "Q4",
            "accession": None,
            "quality": "synthetic_q4",
            "raw": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            "version_filed_at": synthetic["version_filed_at"],
        })
    return result


def derive_metric_rows(company: CompanyTicker, fact_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
    for row in fact_rows:
        groups.setdefault((int(row["fiscal_year"]), row["fiscal_period"], row["period_end"]), []).append(row)
    latest_by_fy_fp: dict[tuple[int, str], tuple[int, str, str]] = {}
    for key in groups:
        fiscal_year, fiscal_period, period_end = key
        current = latest_by_fy_fp.get((fiscal_year, fiscal_period))
        if current is None or period_end > current[2]:
            latest_by_fy_fp[(fiscal_year, fiscal_period)] = key

    derived_rows = []
    for key, period_rows in groups.items():
        fiscal_year, fiscal_period, period_end = key
        facts = facts_for_period(period_rows)
        prior_key = latest_by_fy_fp.get((fiscal_year - 1, fiscal_period))
        prior_facts = facts_for_period(groups.get(prior_key, [])) if prior_key else {}
        derived = calculate_derived_metrics(facts, prior_facts)
        source_rows = [row for row in period_rows if row["metric"] in facts]
        version_filed_at = max((row["version_filed_at"] for row in source_rows), default=period_end)
        filed_at = max((row["filed_at"] for row in source_rows), default=period_end)
        accession = max((str(row.get("accession") or "") for row in source_rows), default="")
        form = max((str(row.get("form") or "") for row in source_rows), default="")
        for metric, item in derived.items():
            raw = dict(item.get("raw") or {})
            raw.setdefault("source_accessions", unique_strings(row.get("accession") for row in source_rows if row.get("accession")))
            raw.setdefault("source_metrics", sorted(facts.keys()))
            quality = str(raw.get("quality") or "available")
            derived_rows.append({
                "symbol": company.symbol,
                "cik": company.cik,
                "metric": metric,
                "value": float_value(item.get("value")),
                "fiscal_year": fiscal_year,
                "fiscal_period": fiscal_period,
                "period_end": period_end,
                "form": form,
                "accession": accession or None,
                "filed_at": filed_at,
                "quality": quality,
                "raw": json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str),
                "version_filed_at": version_filed_at,
            "computed_at": clickhouse_now(),
            })
    return sorted(derived_rows, key=lambda item: (item["symbol"], item["metric"], item["fiscal_year"], item["fiscal_period"], item["period_end"]))


def facts_for_period(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for row in rows:
        metric = row["metric"]
        current = result.get(metric)
        if current is None or row_sort_key(row) > row_sort_key(current):
            result[metric] = row_to_metric_fact(row)
    return result


def row_to_metric_fact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric": row.get("metric"),
        "value": Decimal(str(row.get("value") if row.get("value") is not None else "0")),
        "fy": row.get("fiscal_year"),
        "fp": row.get("fiscal_period"),
        "form": row.get("form"),
        "filed_at": row.get("filed_at"),
        "period_end": row.get("period_end"),
        "accession": row.get("accession"),
        "raw": parse_json(row.get("raw")),
    }


def normalize_frame_payload(payload: dict[str, Any], filename: str, by_cik: dict[str, list[CompanyTicker]], frame_concepts: set[str]) -> list[dict[str, Any]]:
    taxonomy, concept, unit, frame_period = frame_metadata(payload, filename)
    if frame_concepts and concept not in frame_concepts:
        return []
    rows = []
    for item in payload.get("data") or []:
        raw_cik = item.get("cik") or item.get("CIK")
        if raw_cik is None:
            continue
        try:
            cik = normalize_cik(str(raw_cik))
        except ValueError:
            continue
        companies = by_cik.get(cik) or []
        if not companies:
            continue
        filed_at = date_string(item.get("filed")) or date_string(item.get("filed_at")) or date_string(item.get("end"))
        if not filed_at:
            continue
        for company in companies:
            rows.append({
                "frame_period": str(item.get("frame") or frame_period or ""),
                "taxonomy": taxonomy,
                "concept": concept,
                "unit": unit,
                "symbol": company.symbol,
                "cik": company.cik,
                "value": float_value(item.get("val") if "val" in item else item.get("value")),
                "accession": str(item.get("accn") or item.get("accession") or ""),
                "filed_at": filed_at,
                "quality": "frame_as_reported",
                "raw": json.dumps({
                    "quality": "frame_as_reported",
                    "entityName": item.get("entityName") or company.company_name,
                    "loc": item.get("loc"),
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "fy": item.get("fy"),
                    "fp": item.get("fp"),
                    "form": item.get("form"),
                    "sourceFile": filename,
                }, ensure_ascii=False, separators=(",", ":"), default=str),
            })
    return rows


def frame_metadata(payload: dict[str, Any], filename: str) -> tuple[str, str, str, str]:
    parts = filename.replace("\\", "/").split("/")
    taxonomy = str(payload.get("taxonomy") or payload.get("tax") or "us-gaap")
    concept = str(payload.get("tag") or payload.get("concept") or "")
    unit = str(payload.get("uom") or payload.get("unit") or "")
    frame_period = str(payload.get("ccp") or payload.get("frame") or "")
    if not concept:
        for part in parts:
            if part.endswith(".json"):
                concept = part[:-5]
                break
    if not unit:
        unit = "USD"
    return taxonomy, concept, unit, frame_period


def write_redis_summary(redis_client: Any, symbol: str, fact_rows: list[dict[str, Any]], derived_rows: list[dict[str, Any]], *, ttl_seconds: int = 0) -> bool:
    if redis_client is None:
        return False
    payload = build_summary_payload(symbol, fact_rows, derived_rows)
    if not payload:
        return False
    redis_write_json(redis_client, fundamentals_summary_key(symbol), payload, ttl_seconds=ttl_seconds)
    return True


def write_redis_peer_summaries(redis_client: Any, frame_rows: list[dict[str, Any]], *, ttl_seconds: int = 0) -> int:
    if redis_client is None or not frame_rows:
        return 0
    symbols = unique_strings(row.get("symbol") for row in frame_rows)
    written = 0
    for symbol in symbols:
        frames = peer_frame_groups_for_symbol(symbol, frame_rows)
        if write_redis_peer_summary(redis_client, symbol, frames, ttl_seconds=ttl_seconds):
            written += 1
    return written


def peer_frame_groups_for_symbol(symbol: str, frame_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    target = str(symbol or "").strip().upper()
    symbol_rows = [row for row in frame_rows if str(row.get("symbol") or "").strip().upper() == target]
    if not symbol_rows:
        return []
    latest_for_symbol = latest_peer_frame_groups(symbol_rows)
    rows_by_frame_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in frame_rows:
        key = (
            str(row.get("frame_period") or ""),
            str(row.get("concept") or ""),
            str(row.get("unit") or ""),
        )
        rows_by_frame_key.setdefault(key, []).append(row)
    frames = []
    for frame in latest_for_symbol:
        key = (
            str(frame.get("frame_period") or ""),
            str(frame.get("concept") or ""),
            str(frame.get("unit") or ""),
        )
        frames.append({**frame, "rows": sorted(rows_by_frame_key.get(key, []), key=lambda row: str(row.get("symbol") or ""))})
    return frames


def latest_peer_frame_groups(frame_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in frame_rows:
        concept = str(row.get("concept") or "")
        unit = str(row.get("unit") or "")
        if not concept or not unit:
            continue
        groups.setdefault((concept, unit), []).append(row)

    frames = []
    for (concept, unit), rows in groups.items():
        latest_period = max((str(row.get("frame_period") or "") for row in rows), key=frame_period_sort_key, default="")
        if not latest_period:
            continue
        latest_rows = [row for row in rows if str(row.get("frame_period") or "") == latest_period]
        if not latest_rows:
            continue
        frames.append({
            "frame_period": latest_period,
            "concept": concept,
            "unit": unit,
            "rows": sorted(latest_rows, key=lambda row: str(row.get("symbol") or "")),
        })
    return sorted(frames, key=lambda frame: (FRAME_CONCEPT_PRIORITY.get(str(frame.get("concept") or ""), 100), str(frame.get("concept") or "")))


def write_redis_peer_summary(redis_client: Any, symbol: str, frames: list[dict[str, Any]], *, ttl_seconds: int = 0) -> bool:
    if redis_client is None or not frames:
        return False
    primary = frames[0]
    primary_period = frame_base_period(str(primary.get("frame_period") or ""))
    frame_payloads = [peer_frame_payload(symbol, frame) for frame in frames]
    frame_payloads = [frame for frame in frame_payloads if frame.get("peers")]
    if not frame_payloads:
        return False
    payload = {
        "symbol": symbol,
        "summary": f"{symbol} SEC frames 기준 peer 비교 데이터 {len(frame_payloads)}개 묶음을 확인했습니다.",
        "frame_period": primary_period,
        "frame_periods": unique_strings(frame_base_period(str(frame.get("frame_period") or "")) for frame in frame_payloads),
        "concept": primary.get("concept"),
        "unit": primary.get("unit"),
        "frames": frame_payloads,
        "peers": frame_payloads[0].get("peers", []),
        "quality": "frame_as_reported",
        "computed_at": utc_iso(),
    }
    redis_write_json(redis_client, fundamentals_peer_latest_key(symbol), payload, ttl_seconds=ttl_seconds)
    redis_write_json(redis_client, fundamentals_peer_key(symbol, primary_period), payload, ttl_seconds=ttl_seconds)
    return True


def peer_frame_payload(symbol: str, frame: dict[str, Any]) -> dict[str, Any]:
    rows = list(frame.get("rows") or [])
    peers = [
        {
            "symbol": row.get("symbol"),
            "concept": row.get("concept"),
            "value": row.get("value"),
            "unit": row.get("unit"),
            "quality": row.get("quality") or "frame_as_reported",
        }
        for row in target_first_rows(symbol, rows, limit=200)
    ]
    return {
        "frame_period": str(frame.get("frame_period") or ""),
        "display_period": frame_base_period(str(frame.get("frame_period") or "")),
        "concept": frame.get("concept"),
        "unit": frame.get("unit"),
        "peers": peers,
    }


def target_first_rows(symbol: str, rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    target = str(symbol or "").strip().upper()
    target_rows = [row for row in rows if str(row.get("symbol") or "").strip().upper() == target]
    other_rows = [row for row in rows if str(row.get("symbol") or "").strip().upper() != target]
    return [*target_rows, *other_rows][:limit]


def build_summary_payload(symbol: str, fact_rows: list[dict[str, Any]], derived_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    available_rows = [row for row in derived_rows if row.get("value") is not None]
    available_facts = [row for row in fact_rows if row.get("value") is not None]
    basis_rows = available_rows or available_facts
    if not basis_rows:
        return None
    latest = max(basis_rows, key=lambda row: (str(row.get("version_filed_at") or row.get("filed_at") or ""), int(row.get("fiscal_year") or 0), PERIOD_ORDER.get(str(row.get("fiscal_period") or ""), 0)))
    latest_facts = latest_metric_rows(available_facts)
    latest_derived = latest_metric_rows(available_rows)
    metrics = [summary_fact_metric(row) for row in sorted(latest_facts, key=lambda item: str(item.get("metric") or ""))]
    metrics.extend(summary_derived_metric(row) for row in sorted(latest_derived, key=lambda item: str(item.get("metric") or "")))
    warnings = unique_strings(row.get("quality") for row in [*latest_facts, *latest_derived] if row.get("quality") not in {"", None, "available"})
    return {
        "symbol": symbol,
        "cik": latest.get("cik"),
        "summary": f"{symbol} SEC 재무 지표 {len(metrics)}개를 확인했습니다.",
        "latest_period": " ".join(str(item) for item in (latest.get("fiscal_year"), latest.get("fiscal_period")) if item),
        "source": "sec_companyfacts",
        "source_accession": latest.get("accession"),
        "source_filed_at": latest.get("filed_at"),
        "as_of": latest.get("period_end"),
        "computed_at": utc_iso(),
        "metrics": metrics,
        "warnings": warnings,
        "cache_hit": False,
    }


def latest_metric_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_metric: dict[str, dict[str, Any]] = {}
    for row in rows:
        metric = str(row.get("metric") or "")
        if not metric:
            continue
        current = latest_by_metric.get(metric)
        if current is None or summary_row_sort_key(row) > summary_row_sort_key(current):
            latest_by_metric[metric] = row
    return list(latest_by_metric.values())


def summary_row_sort_key(row: dict[str, Any]) -> tuple[str, int, int, str]:
    return (
        str(row.get("version_filed_at") or row.get("filed_at") or ""),
        int(row.get("fiscal_year") or 0),
        PERIOD_ORDER.get(str(row.get("fiscal_period") or ""), 0),
        str(row.get("period_end") or ""),
    )


def summary_fact_metric(row: dict[str, Any]) -> dict[str, Any]:
    raw = parse_json(row.get("raw"))
    return {
        "kind": "fact",
        "metric": row.get("metric"),
        "value": row.get("value"),
        "fiscalYear": row.get("fiscal_year"),
        "fiscalPeriod": row.get("fiscal_period"),
        "periodEnd": row.get("period_end"),
        "asOf": row.get("period_end"),
        "taxonomy": row.get("taxonomy"),
        "concept": row.get("concept"),
        "unit": row.get("unit"),
        "cik": row.get("cik"),
        "form": row.get("form"),
        "accession": row.get("accession"),
        "filedAt": row.get("filed_at"),
        "source": "sec_companyfacts",
        "quality": row.get("quality"),
        "selectedConcept": raw.get("selected_concept"),
    }


def summary_derived_metric(row: dict[str, Any]) -> dict[str, Any]:
    raw = parse_json(row.get("raw"))
    return {
        "kind": "derived",
        "metric": row.get("metric"),
        "value": row.get("value"),
        "fiscalYear": row.get("fiscal_year"),
        "fiscalPeriod": row.get("fiscal_period"),
        "periodEnd": row.get("period_end"),
        "asOf": row.get("period_end"),
        "cik": row.get("cik"),
        "form": row.get("form"),
        "accession": row.get("accession"),
        "filedAt": row.get("filed_at"),
        "source": "sec_companyfacts_derived",
        "quality": row.get("quality"),
        "sourceMetrics": raw.get("source_metrics"),
    }


def parse_company_tickers_exchange(payload: dict[str, Any]) -> dict[str, CompanyTicker]:
    mapping: dict[str, CompanyTicker] = {}
    fields = [str(field).lower() for field in payload.get("fields", [])]
    for row in payload.get("data", []):
        if isinstance(row, dict):
            cik = row.get("cik") or row.get("cik_str")
            name = row.get("name") or row.get("title") or ""
            ticker = row.get("ticker") or row.get("symbol") or ""
            exchange = row.get("exchange") or ""
        else:
            values = list(row)
            cik = value_by_field(values, fields, "cik")
            name = value_by_field(values, fields, "name")
            ticker = value_by_field(values, fields, "ticker")
            exchange = value_by_field(values, fields, "exchange")
        symbol = normalize_symbol(ticker)
        if not symbol or cik is None:
            continue
        try:
            normalized_cik = normalize_cik(str(cik))
        except ValueError:
            continue
        company = CompanyTicker(symbol=symbol, cik=normalized_cik, company_name=str(name or ""), exchange=str(exchange or ""))
        for alias in sec_symbol_aliases(symbol):
            mapping[alias] = company
    return mapping


def resolve_companies(symbols: list[str], ticker_map: dict[str, CompanyTicker]) -> tuple[dict[str, CompanyTicker], list[str]]:
    resolved = {}
    unmatched = []
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        company = None
        for alias in sec_symbol_aliases(normalized):
            company = ticker_map.get(alias)
            if company:
                break
        if company is None:
            unmatched.append(normalized)
            continue
        resolved[normalized] = CompanyTicker(symbol=normalized, cik=company.cik, company_name=company.company_name, exchange=company.exchange)
    return resolved, unmatched


def companies_by_cik(company_map: dict[str, CompanyTicker]) -> dict[str, list[CompanyTicker]]:
    by_cik: dict[str, list[CompanyTicker]] = {}
    for company in company_map.values():
        by_cik.setdefault(company.cik, []).append(company)
    return by_cik


def insert_company_ticker_rows(clickhouse_client: Any, company_map: dict[str, CompanyTicker], active_symbols: list[str], ticker_payload: dict[str, Any], *, batch_size: int) -> None:
    active = set(active_symbols)
    now = clickhouse_now()
    rows = [
        {
            "symbol": company.symbol,
            "cik": company.cik,
            "company_name": company.company_name,
            "exchange": company.exchange,
            "is_active_universe_member": 1 if company.symbol in active else 0,
            "universe_source": "systems/market-data/config/sp500-universe.json",
            "updated_at": now,
            "raw": json.dumps({"source": "company_tickers_exchange", "fields": ticker_payload.get("fields")}, ensure_ascii=False, separators=(",", ":")),
        }
        for company in company_map.values()
    ]
    insert_batches(clickhouse_client, "sec_company_tickers", rows, batch_size)


def insert_raw_artifact_row(clickhouse_client: Any, config: FundamentalsBackfillConfig, stats: BackfillStats, collected_at: datetime, *, source: str = "zip") -> None:
    if not stats.raw_s3_object:
        return
    is_api = source == "api"
    clickhouse_client.insert_json_each_row("sec_raw_artifacts", [{
        "symbol": "_BULK",
        "cik": "",
        "artifact_type": "companyfacts_api" if is_api else "companyfacts_zip",
        "object_path": stats.raw_s3_object,
        "checksum": stats.checksum_sha256,
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/" if is_api else config.companyfacts_zip_url,
        "collected_at": clickhouse_datetime(collected_at),
        "raw": json.dumps({"bucket": config.s3_bucket, "source": "sec_companyfacts_api" if is_api else "sec_bulk_companyfacts"}, ensure_ascii=False, separators=(",", ":")),
    }])


def insert_collection_run(clickhouse_client: Any, stats: BackfillStats, *, status: str, started_at: datetime, finished_at: datetime | None) -> None:
    clickhouse_client.insert_json_each_row("sec_collection_runs", [{
        "run_id": stats.run_id,
        "job_type": "companyfacts_backfill",
        "status": status,
        "symbol_count": int(stats.companies_requested),
        "started_at": clickhouse_datetime(started_at),
        "finished_at": clickhouse_datetime(finished_at) if finished_at else None,
        "raw": json.dumps(stats.to_dict(), ensure_ascii=False, separators=(",", ":"), default=str),
    }])


def ensure_sec_clickhouse_schema(clickhouse_client: Any) -> None:
    for ddl in CLICKHOUSE_TABLES.values():
        clickhouse_client.execute(ddl)
    clickhouse_client.execute("ALTER TABLE market_data.sec_financial_facts MODIFY COLUMN IF EXISTS accession Nullable(String)")
    for migration in CLICKHOUSE_COMPATIBILITY_MIGRATIONS:
        clickhouse_client.execute(migration)


def insert_batches(clickhouse_client: Any, table: str, rows: list[dict[str, Any]], batch_size: int) -> None:
    for start in range(0, len(rows), batch_size):
        clickhouse_client.insert_json_each_row(table, rows[start : start + batch_size])


def build_clickhouse_client():
    from market_data.storage.clickhouse_loader import ClickHouseHttpClient

    return ClickHouseHttpClient(
        url=os.getenv("CLICKHOUSE_HTTP_URL", "http://localhost:8123"),
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "alfaka"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
    )


def build_redis_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    import redis

    return redis.from_url(redis_url, decode_responses=True)


def build_s3_client():
    from market_data.common.s3_client import create_s3_client

    return create_s3_client()


def load_universe_symbols(path: str) -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [normalize_symbol(item) for item in payload.get("symbols", []) if normalize_symbol(item)]


def normalize_frame_symbol(symbol: str) -> str:
    return normalize_symbol(symbol).replace("-", ".")


def normalize_symbol(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def unique_symbols(symbols: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def sec_symbol_aliases(symbol: str) -> list[str]:
    normalized = normalize_symbol(symbol)
    aliases = [normalized]
    if "." in normalized:
        aliases.append(normalized.replace(".", "-"))
    if "-" in normalized:
        aliases.append(normalized.replace("-", "."))
    return unique_symbols(aliases)


def cik_from_zip_name(filename: str) -> str | None:
    basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    match = re.fullmatch(r"CIK(\d{10})\.json", basename)
    if match:
        return match.group(1)
    match = re.fullmatch(r"(\d{10})\.json", basename)
    return match.group(1) if match else None


def likely_frame_file(filename: str) -> bool:
    path = filename.replace("\\", "/").lower()
    return "frame" in path and path.endswith(".json")


def is_frame_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("data"), list)


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def default_frame_concepts_csv() -> str:
    return ",".join([
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "OperatingIncomeLoss",
        "NetIncomeLoss",
        "Assets",
        "Liabilities",
        "StockholdersEquity",
        "dei:EntityCommonStockSharesOutstanding",
    ])


def default_frame_periods(now: datetime | None = None, *, count: int = DEFAULT_FRAME_PERIOD_COUNT) -> list[str]:
    current = now.astimezone(timezone.utc).date() if now else utc_now().date()
    year, quarter = latest_comparable_calendar_quarter(current)
    periods = []
    for _ in range(max(1, count)):
        periods.append(f"CY{year}Q{quarter}")
        year, quarter = previous_calendar_quarter(year, quarter)
    return periods


def latest_comparable_calendar_quarter(current: date) -> tuple[int, int]:
    year = current.year
    current_quarter = ((current.month - 1) // 3) + 1
    year, quarter = previous_calendar_quarter(year, current_quarter)
    quarter_end = calendar_quarter_end(year, quarter)
    if current - quarter_end < timedelta(days=FRAME_FILING_LAG_DAYS):
        year, quarter = previous_calendar_quarter(year, quarter)
    return year, quarter


def previous_calendar_quarter(year: int, quarter: int) -> tuple[int, int]:
    if quarter <= 1:
        return year - 1, 4
    return year, quarter - 1


def calendar_quarter_end(year: int, quarter: int) -> date:
    if quarter == 1:
        return date(year, 3, 31)
    if quarter == 2:
        return date(year, 6, 30)
    if quarter == 3:
        return date(year, 9, 30)
    return date(year, 12, 31)


def frame_periods_for_concept(concept: str, periods: list[str]) -> list[str]:
    normalized = []
    for period in periods:
        text = str(period or "").strip().upper()
        if not text:
            continue
        if concept in INSTANT_FRAME_CONCEPTS and re.fullmatch(r"CY\d{4}Q[1-4]", text):
            text = f"{text}I"
        if concept not in INSTANT_FRAME_CONCEPTS and re.fullmatch(r"CY\d{4}Q[1-4]I", text):
            text = text[:-1]
        if text not in normalized:
            normalized.append(text)
    return normalized


def frame_unit_for_concept(concept: str) -> str:
    return "shares" if concept in SHARE_FRAME_CONCEPTS else "USD"


def frame_concept_sort_key(concept_ref: str) -> tuple[int, str]:
    _taxonomy, concept = parse_concept_ref(concept_ref)
    return FRAME_CONCEPT_PRIORITY.get(concept, 100), concept


def frame_period_sort_key(period: str) -> tuple[int, int, int, str]:
    text = str(period or "").strip().upper()
    match = re.fullmatch(r"CY(\d{4})(?:Q([1-4])(I?)|(I?))?", text)
    if not match:
        return (0, 0, 0, text)
    year = int(match.group(1))
    quarter = int(match.group(2) or "0")
    instant = 1 if (match.group(3) or match.group(4)) == "I" else 0
    return (year, quarter, instant, text)


def frame_base_period(period: str) -> str:
    text = str(period or "").strip().upper()
    if re.fullmatch(r"CY\d{4}Q[1-4]I", text):
        return text[:-1]
    return text


def bool_env(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def float_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def date_string(value: Any) -> str:
    text = str(value or "").strip()
    return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text) else ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def clickhouse_now() -> str:
    return clickhouse_datetime(utc_now())


def clickhouse_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    raw = parse_json(row.get("raw"))
    priority = int(raw.get("concept_priority") if raw.get("concept_priority") is not None else 999)
    return (-priority, str(row.get("filed_at") or ""), str(row.get("accession") or ""))


def parse_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def unique_strings(values: Iterable[Any]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def value_by_field(values: list[Any], fields: list[str], field: str) -> Any:
    try:
        index = fields.index(field)
    except ValueError:
        return None
    return values[index] if index < len(values) else None


def redis_write_json(redis_client: Any, key: str, payload: dict[str, Any], *, ttl_seconds: int) -> None:
    value = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if ttl_seconds > 0:
        redis_client.setex(key, ttl_seconds, value)
    else:
        redis_client.set(key, value)
