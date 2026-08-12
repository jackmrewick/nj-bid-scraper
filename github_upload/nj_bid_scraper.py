#!/usr/bin/env python3
"""
NJ Bid Intelligence Scraper

A repeatable public bid intelligence system for counties + municipalities.

What this does:
- Reads configured state, county, and municipal bid sources from config/sources.yaml.
- Reads prioritized trade rules and scoring weights from config/trade_rules.yaml.
- Scrapes public HTML pages and PDF notices.
- Follows public bid/detail/document links.
- Extracts structured fields with deterministic code.
- Scores listings based on priority trades, open status, future due dates, documents,
  contact info, source quality, and completeness.
- Produces CSV, HTML, JSON, and source coverage reports.

This version intentionally does not require AI. You can add an AI extraction layer later
after the scraper reliably collects raw pages and documents.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None


DEFAULT_USER_AGENT = "NJBidIntelligenceScraper/2.0 contact=your-email@example.com"

DATE_PATTERNS = [
    r"(?:bid(?:s)?|proposal(?:s)?|quote(?:s)?|submission(?:s)?|responses?)\s*(?:are\s*)?(?:due|received|opened|opening|close|closing|deadline)?\s*(?:date|on|by|at|:)?\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}(?:\s+(?:at\s*)?\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(?:bid(?:s)?|proposal(?:s)?|quote(?:s)?|submission(?:s)?|responses?)\s*(?:are\s*)?(?:due|received|opened|opening|close|closing|deadline)?\s*(?:date|on|by|at|:)?\s*(\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(?:closing|opening|due|deadline|receipt)\s*(?:date|on|by|at|:)?\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}(?:\s+(?:at\s*)?\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(?:closing|opening|due|deadline|receipt)\s*(?:date|on|by|at|:)?\s*(\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))",
]

PREBID_PATTERNS = [
    r"(?:mandatory\s*)?(?:pre[-\s]?bid|prebid|site visit|walkthrough|walk-through|pre-proposal).{0,140}?([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}(?:\s+(?:at\s*)?\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(?:mandatory\s*)?(?:pre[-\s]?bid|prebid|site visit|walkthrough|walk-through|pre-proposal).{0,140}?(\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
]

BID_NUMBER_PATTERNS = [
    r"\b(?:bid|rfp|rfq|proposal|contract|project)\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Za-z0-9\-_.\/]+)",
    r"\b(?:spec|specification)\s*(?:no\.?|number|#)\s*[:#]?\s*([A-Za-z0-9\-_.\/]+)",
]

EMAIL_PATTERN = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
PHONE_PATTERN = r"(?:\(\d{3}\)\s*|\d{3}[-.\s])\d{3}[-.\s]\d{4}"


@dataclasses.dataclass
class Source:
    id: str
    name: str
    county: str
    municipality: str
    url: str
    source_type: str
    scraper_type: str
    source_quality: str = "medium"
    active: bool = True


@dataclasses.dataclass
class Candidate:
    source_id: str
    source_name: str
    source_county: str
    source_municipality: str
    source_url: str
    source_type: str
    scraper_type: str
    source_quality: str
    title: str
    detail_url: str
    raw_text: str
    linked_documents: List[str]


@dataclasses.dataclass
class Listing:
    run_id: str
    source_id: str
    source_name: str
    source_county: str
    source_municipality: str
    source_url: str
    source_type: str
    scraper_type: str
    source_quality: str
    title: str
    detail_url: str
    county: Optional[str]
    municipality: Optional[str]
    trade_category: Optional[str]
    matched_trades: List[str]
    trade_tier: str
    status: Optional[str]
    bid_number: Optional[str]
    due_date_raw: Optional[str]
    due_date_iso: Optional[str]
    prebid_date_raw: Optional[str]
    prebid_date_iso: Optional[str]
    summary: str
    contact_info: str
    linked_documents: List[str]
    document_count: int
    has_addenda: bool
    construction_relevance: int
    confidence_score: int
    information_completeness: int
    score: int
    score_band: str
    action_level: str
    score_reasons: List[str]
    missing_fields: List[str]
    raw_text: str
    raw_hash: str
    dedupe_key: str
    scraped_at: str


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def truncate(text: str, limit: int = 500) -> str:
    text = normalize_ws(text)
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def parse_csv_arg(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def lower_set(values: Iterable[str]) -> set[str]:
    return {str(v).strip().lower() for v in values if str(v).strip()}


def contains_any(text: str, words: Iterable[str]) -> bool:
    lower = text.lower()
    return any(w.lower() in lower for w in words)


def count_matches(text: str, words: Iterable[str]) -> int:
    lower = text.lower()
    return sum(1 for w in words if w.lower() in lower)


def domain(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


def same_domain(a: str, b: str) -> bool:
    return domain(a) == domain(b)


def is_pdf(url: str, content_type: str = "") -> bool:
    return content_type == "application/pdf" or urlparse(url).path.lower().endswith(".pdf")


def load_yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def default_rules() -> Dict[str, Any]:
    return {
        "bid_words": ["bid", "rfp", "rfq", "proposal", "notice to bidders", "sealed bids"],
        "open_words": ["open", "active", "accepting"],
        "negative_words": ["closed", "canceled", "cancelled", "expired", "awarded"],
        "document_words": ["plan", "spec", "pdf", "addendum", "bid form"],
        "contact_words": ["contact", "email", "phone", "purchasing"],
        "high_priority_trades": {},
        "core_trades": {},
        "scoring": {},
    }


def load_rules(path: Path) -> Dict[str, Any]:
    rules = default_rules()
    if path.exists():
        loaded = load_yaml(path)
        for k, v in loaded.items():
            rules[k] = v
    rules.setdefault("high_priority_trades", {})
    rules.setdefault("core_trades", {})
    rules.setdefault("scoring", {})
    return rules


def load_sources(config_path: Path) -> Tuple[Dict[str, Any], List[Source]]:
    cfg = load_yaml(config_path)
    settings = cfg.get("settings", {})
    sources: List[Source] = []

    for row in cfg.get("sources", []):
        s = Source(
            id=str(row["id"]),
            name=str(row["name"]),
            county=str(row.get("county", "")),
            municipality=str(row.get("municipality") or ""),
            url=str(row["url"]),
            source_type=str(row.get("source_type", "")),
            scraper_type=str(row.get("scraper_type", "generic_html")),
            source_quality=str(row.get("source_quality", "medium")),
            active=bool(row.get("active", True)),
        )
        if s.active:
            sources.append(s)

    return settings, sources


def filter_sources(
    sources: List[Source],
    counties: Sequence[str],
    municipalities: Sequence[str],
    source_types: Sequence[str],
    include_statewide: bool,
) -> List[Source]:
    county_filter = lower_set(counties)
    municipality_filter = lower_set(municipalities)
    type_filter = lower_set(source_types)

    out: List[Source] = []
    for s in sources:
        county_l = s.county.strip().lower()
        mun_l = s.municipality.strip().lower()
        type_l = s.source_type.strip().lower()

        if type_filter and type_l not in type_filter:
            continue

        if county_filter:
            if county_l == "statewide" and not include_statewide:
                continue
            if county_l != "statewide" and county_l not in county_filter:
                continue

        if municipality_filter:
            # Keep county/state sources only when include_statewide=True; otherwise municipal-only filtering is strict.
            if mun_l and mun_l not in municipality_filter:
                continue
            if not mun_l and not include_statewide:
                continue

        out.append(s)

    return out


def setup_logging(log_dir: Path, run_id: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"scraper_{run_id}.log"

    # Reset handlers so reruns inside Streamlit do not duplicate logs.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_path


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def fetch(s: requests.Session, url: str, timeout: int) -> Tuple[bytes, str, str]:
    logging.info("Fetching %s", url)
    r = s.get(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").split(";")[0].lower()
    return r.content, ctype, r.url


def soup_from(content: bytes) -> BeautifulSoup:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup


def pdf_text(content: bytes) -> str:
    if fitz is None:
        return ""
    parts: List[str] = []
    with fitz.open(stream=content, filetype="pdf") as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    return normalize_ws(" ".join(parts))


def extract_links(soup: BeautifulSoup, base_url: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        text = normalize_ws(a.get_text(" ", strip=True))
        url = urljoin(base_url, a.get("href", ""))
        if url.startswith(("http://", "https://")):
            out.append((text, url))
    return out


def looks_like_bid(text: str, rules: Dict[str, Any]) -> bool:
    text = normalize_ws(text)
    if len(text) < 12:
        return False
    trade_words = []
    for d in (rules.get("high_priority_trades", {}), rules.get("core_trades", {})):
        for words in d.values():
            trade_words.extend(words)
    return (
        contains_any(text, rules.get("bid_words", []))
        or contains_any(text, trade_words)
        or contains_any(text, rules.get("document_words", []))
    )


def link_useful(text: str, url: str, rules: Dict[str, Any]) -> bool:
    c = f"{text} {url}".lower()
    all_words = (
        rules.get("bid_words", [])
        + rules.get("document_words", [])
        + ["pdf", ".pdf", "download", "document", "detail", "notice", "addendum", "specification"]
    )
    trade_words = []
    for d in (rules.get("high_priority_trades", {}), rules.get("core_trades", {})):
        for words in d.values():
            trade_words.extend(words)
    return any(w.lower() in c for w in all_words + trade_words)


def blocks(soup: BeautifulSoup) -> List[Tuple[str, Optional[str], List[str]]]:
    rows: List[Tuple[str, Optional[str], List[str]]] = []
    for tag_name in ["tr", "li", "article", "section", "div"]:
        for block in soup.find_all(tag_name, limit=800):
            text = normalize_ws(block.get_text(" ", strip=True))
            if len(text) < 20:
                continue
            hrefs = [a.get("href", "") for a in block.find_all("a", href=True)]
            rows.append((text, hrefs[0] if hrefs else None, hrefs))

    seen = set()
    unique: List[Tuple[str, Optional[str], List[str]]] = []
    for text, first, hrefs in rows:
        key = sha(text[:1200])
        if key not in seen:
            seen.add(key)
            unique.append((text, first, hrefs))
    return unique


def derive_title(text: str, rules: Dict[str, Any]) -> str:
    text = normalize_ws(text)
    label_patterns = [
        r"Bid Title\s*[:\-]\s*(.{10,180})",
        r"Project\s*(?:Name|Title)?\s*[:\-]\s*(.{10,180})",
        r"Title\s*[:\-]\s*(.{10,180})",
        r"Notice to Bidders\s*[:\-]?\s*(.{10,180})",
        r"Description\s*[:\-]\s*(.{10,180})",
    ]
    for pattern in label_patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return truncate(m.group(1), 160)

    for chunk in re.split(r"(?<=[.!?])\s+|\s{3,}|\|", text[:1800]):
        chunk = normalize_ws(chunk)
        if 15 <= len(chunk) <= 180 and looks_like_bid(chunk, rules):
            return truncate(chunk, 160)

    return truncate(text, 160)


def enrich_detail(
    s: requests.Session,
    base_url: str,
    detail_url: str,
    timeout: int,
    rules: Dict[str, Any],
    same_domain_only: bool,
) -> Tuple[str, List[str]]:
    try:
        if same_domain_only and not same_domain(base_url, detail_url) and not detail_url.lower().endswith(".pdf"):
            return "", []

        content, ctype, final = fetch(s, detail_url, timeout)
        if is_pdf(final, ctype):
            return pdf_text(content), [final]

        sp = soup_from(content)
        text = normalize_ws(sp.get_text(" ", strip=True))
        docs = [u for t, u in extract_links(sp, final) if link_useful(t, u, rules)]
        return text, list(dict.fromkeys(docs))[:30]
    except Exception as e:
        logging.warning("Could not enrich %s: %s", detail_url, e)
        return "", []


def candidate_from_page(
    source: Source,
    final_url: str,
    title: str,
    raw_text: str,
    linked_documents: List[str],
) -> Candidate:
    return Candidate(
        source_id=source.id,
        source_name=source.name,
        source_county=source.county,
        source_municipality=source.municipality,
        source_url=source.url,
        source_type=source.source_type,
        scraper_type=source.scraper_type,
        source_quality=source.source_quality,
        title=title,
        detail_url=final_url,
        raw_text=raw_text,
        linked_documents=linked_documents,
    )


def scrape_generic(
    s: requests.Session,
    source: Source,
    timeout: int,
    max_detail: int,
    rules: Dict[str, Any],
    same_domain_only: bool,
) -> List[Candidate]:
    content, ctype, final = fetch(s, source.url, timeout)

    if is_pdf(final, ctype):
        text = pdf_text(content)
        if looks_like_bid(text, rules):
            return [candidate_from_page(source, final, derive_title(text, rules) or source.name, text, [final])]
        return []

    sp = soup_from(content)
    out: List[Candidate] = []

    page_text = normalize_ws(sp.get_text(" ", strip=True))
    page_docs = [u for t, u in extract_links(sp, final) if link_useful(t, u, rules)]
    if looks_like_bid(page_text, rules):
        out.append(candidate_from_page(source, final, source.name, page_text, page_docs[:30]))

    enriched = 0
    for text, first, hrefs in blocks(sp):
        if not looks_like_bid(text, rules):
            continue

        links = [urljoin(final, h) for h in hrefs if h]
        detail = urljoin(final, first) if first else final

        extra, docs = "", []
        if detail != final and enriched < max_detail:
            extra, docs = enrich_detail(s, final, detail, timeout, rules, same_domain_only)
            enriched += 1

        full = normalize_ws(f"{text} {extra}")
        if looks_like_bid(full, rules):
            out.append(candidate_from_page(
                source,
                detail,
                derive_title(full, rules),
                full,
                list(dict.fromkeys(links + docs))[:40],
            ))

    return out


def scrape_civic(
    s: requests.Session,
    source: Source,
    timeout: int,
    max_detail: int,
    rules: Dict[str, Any],
    same_domain_only: bool,
) -> List[Candidate]:
    content, ctype, final = fetch(s, source.url, timeout)
    sp = soup_from(content)
    out: List[Candidate] = []
    enriched = 0

    civic_hints = [
        "bid title", "category", "status", "description", "publication date",
        "closing date", "submittal information", "bid recipient", "related documents",
    ]

    for text, first, hrefs in blocks(sp):
        lower = text.lower()
        civic_hint = any(h in lower for h in civic_hints)
        if not (civic_hint or looks_like_bid(text, rules)):
            continue

        links = [urljoin(final, h) for h in hrefs if h]
        detail = urljoin(final, first) if first else final

        extra, docs = "", []
        if detail != final and enriched < max_detail:
            extra, docs = enrich_detail(s, final, detail, timeout, rules, same_domain_only)
            enriched += 1

        full = normalize_ws(f"{text} {extra}")
        if looks_like_bid(full, rules):
            out.append(candidate_from_page(
                source,
                detail,
                derive_title(full, rules),
                full,
                list(dict.fromkeys(links + docs))[:40],
            ))

    return out


def scrape_pdf_direct(
    s: requests.Session,
    source: Source,
    timeout: int,
    max_detail: int,
    rules: Dict[str, Any],
    same_domain_only: bool,
) -> List[Candidate]:
    content, ctype, final = fetch(s, source.url, timeout)
    text = pdf_text(content) if is_pdf(final, ctype) else normalize_ws(soup_from(content).get_text(" ", strip=True))
    if looks_like_bid(text, rules):
        return [candidate_from_page(source, final, derive_title(text, rules) or source.name, text, [final])]
    return []


def scrape_source(
    s: requests.Session,
    source: Source,
    timeout: int,
    max_detail: int,
    rules: Dict[str, Any],
    same_domain_only: bool,
) -> List[Candidate]:
    if source.scraper_type == "civicengage":
        return scrape_civic(s, source, timeout, max_detail, rules, same_domain_only)
    if source.scraper_type == "pdf_direct":
        return scrape_pdf_direct(s, source, timeout, max_detail, rules, same_domain_only)
    if source.scraper_type == "generic_html":
        return scrape_generic(s, source, timeout, max_detail, rules, same_domain_only)
    raise ValueError(f"Unknown scraper_type: {source.scraper_type}")


def parse_date(text: str, patterns: List[str]) -> Tuple[Optional[str], Optional[str]]:
    for p in patterns:
        m = re.search(p, text, flags=re.I | re.S)
        if not m:
            continue
        raw = normalize_ws(m.group(1)).replace(" at ", " ")
        try:
            parsed = date_parser.parse(raw, fuzzy=True)
            return raw, parsed.isoformat(timespec="minutes")
        except Exception:
            pass
    return None, None


def bid_number(text: str) -> Optional[str]:
    for pattern in BID_NUMBER_PATTERNS:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return truncate(m.group(1), 80)
    return None


def infer_county(text: str, source_county: str, targets: List[str]) -> Optional[str]:
    if source_county and source_county.lower() != "statewide":
        return source_county
    lower = text.lower()
    for c in targets:
        if re.search(rf"\b{re.escape(c.lower())}\b", lower):
            return c
    return source_county if source_county and source_county.lower() != "statewide" else None


def infer_municipality(text: str, source_municipality: str, target_municipalities: List[str]) -> Optional[str]:
    if source_municipality:
        return source_municipality
    lower = text.lower()
    for m in target_municipalities:
        if re.search(rf"\b{re.escape(m.lower())}\b", lower):
            return m
    return None


def trade_matches(text: str, rules: Dict[str, Any]) -> Tuple[Optional[str], List[str], str, int]:
    lower = text.lower()
    scored: List[Tuple[str, str, int]] = []

    for tier_name, groups in [
        ("High Priority", rules.get("high_priority_trades", {})),
        ("Core", rules.get("core_trades", {})),
    ]:
        for trade, words in groups.items():
            score = 0
            for w in words:
                wl = str(w).lower()
                if not wl:
                    continue
                if wl in lower:
                    score += 3 if " " in wl else 1
            if score > 0:
                scored.append((trade, tier_name, score))

    if not scored:
        return None, [], "Unmatched", 0

    scored.sort(key=lambda x: (x[1] == "High Priority", x[2]), reverse=True)
    primary_trade, primary_tier, primary_score = scored[0]
    matched = [t for t, _, _ in scored]
    # Keep matched trades unique but ordered.
    matched_unique = list(dict.fromkeys(matched))
    return primary_trade, matched_unique, primary_tier, primary_score


def infer_status(text: str, rules: Dict[str, Any]) -> Optional[str]:
    lower = text.lower()
    negatives = rules.get("negative_words", [])
    if any(w in lower for w in ["canceled", "cancelled", "withdrawn"]):
        return "Canceled"
    if any(w in lower for w in ["closed", "expired", "no longer accepting", "not accepting"]):
        return "Closed"
    if "awarded" in lower or "award notice" in lower or "bid results" in lower:
        return "Awarded"
    if contains_any(text, rules.get("open_words", [])):
        return "Open"
    return None


def contact_info(text: str) -> str:
    found = list(dict.fromkeys(re.findall(EMAIL_PATTERN, text) + re.findall(PHONE_PATTERN, text)))
    return "; ".join(found[:8])


def document_signals(text: str, docs: List[str], rules: Dict[str, Any]) -> Tuple[bool, bool]:
    doc_text = " ".join(docs) + " " + text
    has_docs = bool(docs) or contains_any(doc_text, rules.get("document_words", []))
    has_addenda = "addendum" in doc_text.lower() or "addenda" in doc_text.lower()
    return has_docs, has_addenda


def construction_relevance(text: str, rules: Dict[str, Any]) -> int:
    trade_words: List[str] = []
    for groups in (rules.get("high_priority_trades", {}), rules.get("core_trades", {})):
        for words in groups.values():
            trade_words.extend(words)

    score = 0
    score += min(30, count_matches(text, rules.get("bid_words", [])) * 3)
    score += min(50, count_matches(text, trade_words) * 4)
    score += min(20, count_matches(text, rules.get("document_words", [])) * 2)

    lower = text.lower()
    if "notice to bidders" in lower:
        score += 8
    if "prevailing wage" in lower:
        score += 5
    if "bid bond" in lower or "performance bond" in lower:
        score += 5

    return max(0, min(100, score))


def date_future(iso: Optional[str]) -> Optional[bool]:
    if not iso:
        return None
    try:
        return date_parser.parse(iso).date() >= dt.date.today()
    except Exception:
        return None


def days_until(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return (date_parser.parse(iso).date() - dt.date.today()).days
    except Exception:
        return None


def missing_fields(
    due_iso: Optional[str],
    contact: str,
    docs: List[str],
    detail_url: str,
    trade: Optional[str],
    county: Optional[str],
    status: Optional[str],
) -> List[str]:
    missing = []
    if not due_iso:
        missing.append("due_date")
    if not contact:
        missing.append("contact_info")
    if not docs:
        missing.append("document_links")
    if not detail_url:
        missing.append("website_link")
    if not trade:
        missing.append("trade_match")
    if not county:
        missing.append("county")
    if not status:
        missing.append("status")
    return missing


def completeness_score(missing: List[str], bid_no: Optional[str], prebid_iso: Optional[str]) -> int:
    base = 100
    penalties = {
        "due_date": 25,
        "contact_info": 15,
        "document_links": 15,
        "website_link": 10,
        "trade_match": 20,
        "county": 8,
        "status": 8,
    }
    for field in missing:
        base -= penalties.get(field, 5)
    if bid_no:
        base += 4
    if prebid_iso:
        base += 4
    return max(0, min(100, base))


def confidence_score(
    source_quality: str,
    relevance: int,
    completeness: int,
    trade_strength: int,
    due_iso: Optional[str],
    status: Optional[str],
    docs: List[str],
) -> int:
    score = 0
    score += 15 if source_quality.lower() == "high" else 8 if source_quality.lower() == "medium" else 3
    score += min(25, relevance // 4)
    score += min(25, completeness // 4)
    score += min(15, trade_strength * 2)
    if due_iso:
        score += 10
    if status:
        score += 5
    if docs:
        score += 5
    return max(0, min(100, score))


def score_candidate(
    c: Candidate,
    county: Optional[str],
    municipality: Optional[str],
    trade: Optional[str],
    matched_trades: List[str],
    trade_tier: str,
    trade_strength: int,
    status: Optional[str],
    due_iso: Optional[str],
    prebid_iso: Optional[str],
    bid_no: Optional[str],
    contact: str,
    docs: List[str],
    targets: List[str],
    target_municipalities: List[str],
    relevance: int,
    missing: List[str],
    rules: Dict[str, Any],
) -> Tuple[int, str, str, List[str]]:
    weights = rules.get("scoring", {})
    score = 0
    reasons: List[str] = []

    target_counties = lower_set(targets)
    target_muns = lower_set(target_municipalities)

    if county and (not target_counties or county.lower() in target_counties):
        pts = int(weights.get("target_county", 12))
        score += pts
        reasons.append(f"+{pts} target county: {county}")
    elif c.source_county.lower() == "statewide":
        score += 3
        reasons.append("+3 statewide source")
    else:
        score -= 8
        reasons.append("-8 county not clearly targeted")

    if municipality and (not target_muns or municipality.lower() in target_muns):
        pts = int(weights.get("target_municipality", 8))
        score += pts
        reasons.append(f"+{pts} target municipality: {municipality}")

    if c.source_quality.lower() == "high":
        pts = int(weights.get("source_quality_high", 6))
        score += pts
        reasons.append(f"+{pts} high-quality source")
    elif c.source_quality.lower() == "medium":
        pts = int(weights.get("source_quality_medium", 3))
        score += pts
        reasons.append(f"+{pts} medium-quality source")

    if c.source_type.lower() in {"state", "county", "municipality", "authority", "school"}:
        pts = int(weights.get("official_source", 6))
        score += pts
        reasons.append(f"+{pts} official/public source type: {c.source_type}")

    if trade_tier == "High Priority":
        base = int(weights.get("high_priority_trade_base", 32))
        cap = int(weights.get("high_priority_trade_strength_cap", 14))
        pts = base + min(cap, trade_strength * 2)
        score += pts
        reasons.append(f"+{pts} high-priority trade match: {trade}")
    elif trade_tier == "Core":
        base = int(weights.get("core_trade_base", 16))
        cap = int(weights.get("core_trade_strength_cap", 8))
        pts = base + min(cap, trade_strength * 2)
        score += pts
        reasons.append(f"+{pts} core trade match: {trade}")
    else:
        pts = int(weights.get("no_trade_penalty", -18))
        score += pts
        reasons.append(f"{pts} no prioritized trade match")

    future, days = date_future(due_iso), days_until(due_iso)
    if future is True:
        pts = int(weights.get("future_due_date", 18))
        score += pts
        reasons.append(f"+{pts} future/open due date")
        due_soon_days = int(weights.get("due_soon_days", 21))
        if days is not None and 0 <= days <= due_soon_days:
            bonus = int(weights.get("due_soon_bonus", 8))
            score += bonus
            reasons.append(f"+{bonus} due soon: {days} days away")
    elif future is False:
        pts = int(weights.get("expired_due_date_penalty", -45))
        score += pts
        reasons.append(f"{pts} expired due date")
    else:
        pts = int(weights.get("missing_due_date_penalty", -18))
        score += pts
        reasons.append(f"{pts} no due date found")

    if status in {"Closed", "Canceled", "Awarded"}:
        pts = int(weights.get("closed_status_penalty", -55))
        score += pts
        reasons.append(f"{pts} status is {status}")
    elif status == "Open":
        pts = int(weights.get("open_status", 15))
        score += pts
        reasons.append(f"+{pts} status appears open")

    if prebid_iso:
        pts = int(weights.get("prebid_date", 5))
        score += pts
        reasons.append(f"+{pts} pre-bid/site visit date found")
    if bid_no:
        pts = int(weights.get("bid_number", 4))
        score += pts
        reasons.append(f"+{pts} bid/project number found")

    if contact:
        pts = int(weights.get("contact_info", 8))
        score += pts
        reasons.append(f"+{pts} contact info found")

    if docs:
        pts = int(weights.get("document_links", 10))
        score += pts
        reasons.append(f"+{pts} document links found ({len(docs)})")

    if c.detail_url:
        pts = int(weights.get("detail_url", 5))
        score += pts
        reasons.append(f"+{pts} website/detail link found")

    if any("addendum" in d.lower() or "addenda" in d.lower() for d in docs) or "addendum" in c.raw_text.lower() or "addenda" in c.raw_text.lower():
        pts = int(weights.get("addenda_found", 5))
        score += pts
        reasons.append(f"+{pts} addenda signal found")

    if relevance >= 70:
        pts = int(weights.get("strong_bid_language", 10))
        score += pts
        reasons.append(f"+{pts} strong bid/trade language")
    elif relevance >= 40:
        pts = int(weights.get("moderate_bid_language", 5))
        score += pts
        reasons.append(f"+{pts} moderate bid/trade language")
    elif relevance < 15:
        pts = int(weights.get("weak_bid_language_penalty", -8))
        score += pts
        reasons.append(f"{pts} weak bid language")

    complete_fields = {"due_date", "contact_info", "document_links", "website_link", "trade_match"}
    if not (set(missing) & complete_fields):
        pts = int(weights.get("complete_info_bonus", 10))
        score += pts
        reasons.append(f"+{pts} complete key information")

    if contains_any(c.raw_text, rules.get("negative_words", [])):
        pts = int(weights.get("negative_language_penalty", -18))
        score += pts
        reasons.append(f"{pts} negative/closed/archive language found")

    # The most important rule: an open prioritized bid should surface high.
    if trade_tier == "High Priority" and future is True and status != "Closed":
        score += 12
        reasons.append("+12 high-priority trade with future/open date")

    score = max(0, min(100, score))
    band = "HOT" if score >= 82 else "REVIEW" if score >= 62 else "MONITOR" if score >= 40 else "ARCHIVE"
    if band == "HOT":
        action = "Review for Bid"
    elif band == "REVIEW":
        action = "Follow Up"
    elif band == "MONITOR":
        action = "Monitor"
    else:
        action = "Archive"

    return score, band, action, reasons


def dedupe_key(title: str, source: str, due: Optional[str], county: Optional[str], municipality: Optional[str]) -> str:
    base = f"{title} {source} {due or ''} {county or ''} {municipality or ''}".lower()
    base = re.sub(r"[^a-z0-9 ]+", " ", base)
    base = re.sub(r"\b(bid|bids|rfp|rfq|notice|project|the|and|of|for|to|at)\b", " ", base)
    return hashlib.sha1(normalize_ws(base).encode("utf-8")).hexdigest()


def normalize_candidate(
    c: Candidate,
    run_id: str,
    targets: List[str],
    target_municipalities: List[str],
    rules: Dict[str, Any],
) -> Listing:
    raw = normalize_ws(c.raw_text)
    title = truncate(c.title or derive_title(raw, rules) or c.source_name, 180)
    due_raw, due_iso = parse_date(raw, DATE_PATTERNS)
    pre_raw, pre_iso = parse_date(raw, PREBID_PATTERNS)

    county = infer_county(raw, c.source_county, targets)
    municipality = infer_municipality(raw, c.source_municipality, target_municipalities)
    trade, matched, tier, strength = trade_matches(raw, rules)
    status = infer_status(raw, rules)
    bid_no = bid_number(raw)
    contact = contact_info(raw)
    docs = list(dict.fromkeys(u for u in c.linked_documents if u))[:40]
    rel = construction_relevance(raw, rules)
    missing = missing_fields(due_iso, contact, docs, c.detail_url, trade, county, status)
    complete = completeness_score(missing, bid_no, pre_iso)
    conf = confidence_score(c.source_quality, rel, complete, strength, due_iso, status, docs)
    score, band, action, reasons = score_candidate(
        c, county, municipality, trade, matched, tier, strength, status, due_iso, pre_iso, bid_no,
        contact, docs, targets, target_municipalities, rel, missing, rules
    )
    _, has_addenda = document_signals(raw, docs, rules)

    return Listing(
        run_id=run_id,
        source_id=c.source_id,
        source_name=c.source_name,
        source_county=c.source_county,
        source_municipality=c.source_municipality,
        source_url=c.source_url,
        source_type=c.source_type,
        scraper_type=c.scraper_type,
        source_quality=c.source_quality,
        title=title,
        detail_url=c.detail_url,
        county=county,
        municipality=municipality,
        trade_category=trade,
        matched_trades=matched,
        trade_tier=tier,
        status=status,
        bid_number=bid_no,
        due_date_raw=due_raw,
        due_date_iso=due_iso,
        prebid_date_raw=pre_raw,
        prebid_date_iso=pre_iso,
        summary=truncate(raw, 850),
        contact_info=contact,
        linked_documents=docs,
        document_count=len(docs),
        has_addenda=has_addenda,
        construction_relevance=rel,
        confidence_score=conf,
        information_completeness=complete,
        score=score,
        score_band=band,
        action_level=action,
        score_reasons=reasons,
        missing_fields=missing,
        raw_text=raw,
        raw_hash=sha(c.detail_url + raw),
        dedupe_key=dedupe_key(title, c.source_name, due_iso, county, municipality),
        scraped_at=now_utc(),
    )


def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs(
            run_id TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            source_count INTEGER,
            sources_successful INTEGER,
            sources_failed INTEGER,
            candidates_found INTEGER,
            listings_saved INTEGER,
            duplicates_skipped INTEGER,
            report_csv TEXT,
            report_html TEXT,
            report_json TEXT,
            coverage_csv TEXT,
            log_path TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            source_id TEXT,
            source_name TEXT,
            county TEXT,
            municipality TEXT,
            source_type TEXT,
            url TEXT,
            success INTEGER,
            candidates_found INTEGER,
            error_message TEXT,
            checked_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS listings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            source_id TEXT,
            source_name TEXT,
            source_county TEXT,
            source_municipality TEXT,
            source_url TEXT,
            source_type TEXT,
            scraper_type TEXT,
            source_quality TEXT,
            title TEXT,
            detail_url TEXT,
            county TEXT,
            municipality TEXT,
            trade_category TEXT,
            matched_trades_json TEXT,
            trade_tier TEXT,
            status TEXT,
            bid_number TEXT,
            due_date_raw TEXT,
            due_date_iso TEXT,
            prebid_date_raw TEXT,
            prebid_date_iso TEXT,
            summary TEXT,
            contact_info TEXT,
            linked_documents_json TEXT,
            document_count INTEGER,
            has_addenda INTEGER,
            construction_relevance INTEGER,
            confidence_score INTEGER,
            information_completeness INTEGER,
            score INTEGER,
            score_band TEXT,
            action_level TEXT,
            score_reasons_json TEXT,
            missing_fields_json TEXT,
            raw_text TEXT,
            raw_hash TEXT,
            dedupe_key TEXT,
            scraped_at TEXT,
            UNIQUE(run_id, raw_hash)
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_run ON listings(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(score)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_due ON listings(due_date_iso)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_trade ON listings(trade_category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_area ON listings(county, municipality)")
    conn.commit()
    return conn


def start_run(conn: sqlite3.Connection, run_id: str, source_count: int, log_path: Path) -> None:
    conn.execute(
        "INSERT INTO runs(run_id,started_at,source_count,sources_successful,sources_failed,candidates_found,listings_saved,duplicates_skipped,log_path) VALUES(?,?,?,0,0,0,0,0,?)",
        (run_id, now_utc(), source_count, str(log_path)),
    )
    conn.commit()


def source_result(conn: sqlite3.Connection, run_id: str, s: Source, success: bool, count: int, err: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO source_results(run_id,source_id,source_name,county,municipality,source_type,url,success,candidates_found,error_message,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, s.id, s.name, s.county, s.municipality, s.source_type, s.url, int(success), count, err, now_utc()),
    )
    conn.commit()


def store_listing(conn: sqlite3.Connection, l: Listing) -> bool:
    try:
        conn.execute(
            """
            INSERT INTO listings(
                run_id,source_id,source_name,source_county,source_municipality,source_url,source_type,
                scraper_type,source_quality,title,detail_url,county,municipality,trade_category,
                matched_trades_json,trade_tier,status,bid_number,due_date_raw,due_date_iso,
                prebid_date_raw,prebid_date_iso,summary,contact_info,linked_documents_json,
                document_count,has_addenda,construction_relevance,confidence_score,information_completeness,
                score,score_band,action_level,score_reasons_json,missing_fields_json,raw_text,raw_hash,
                dedupe_key,scraped_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                l.run_id, l.source_id, l.source_name, l.source_county, l.source_municipality,
                l.source_url, l.source_type, l.scraper_type, l.source_quality, l.title, l.detail_url,
                l.county, l.municipality, l.trade_category, json.dumps(l.matched_trades),
                l.trade_tier, l.status, l.bid_number, l.due_date_raw, l.due_date_iso, l.prebid_date_raw,
                l.prebid_date_iso, l.summary, l.contact_info, json.dumps(l.linked_documents),
                l.document_count, int(l.has_addenda), l.construction_relevance, l.confidence_score,
                l.information_completeness, l.score, l.score_band, l.action_level,
                json.dumps(l.score_reasons), json.dumps(l.missing_fields), l.raw_text, l.raw_hash,
                l.dedupe_key, l.scraped_at,
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def df_for_run(conn: sqlite3.Connection, run_id: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT
            score_band, score, action_level, confidence_score, information_completeness,
            title, source_name, county, municipality, trade_category, matched_trades_json,
            trade_tier, status, bid_number, due_date_iso, prebid_date_iso, contact_info,
            detail_url, document_count, has_addenda, summary, construction_relevance,
            score_reasons_json, missing_fields_json, linked_documents_json, dedupe_key, scraped_at
        FROM listings
        WHERE run_id=?
        ORDER BY score DESC, due_date_iso ASC
        """,
        conn,
        params=(run_id,),
    )


def sources_for_run(conn: sqlite3.Connection, run_id: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT source_name, county, municipality, source_type, url, success, candidates_found, error_message, checked_at
        FROM source_results
        WHERE run_id=?
        ORDER BY county, municipality, source_name
        """,
        conn,
        params=(run_id,),
    )


def latest_run_id(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


def explode_json_list(value: Any) -> str:
    try:
        data = json.loads(value or "[]")
        if isinstance(data, list):
            return "; ".join(str(x) for x in data)
    except Exception:
        pass
    return ""


def summary(df: pd.DataFrame, sdf: pd.DataFrame, run_id: str) -> Dict[str, Any]:
    if df.empty:
        return {
            "run_id": run_id,
            "total_listings": 0,
            "message": "No listings saved.",
        }

    due_soon = 0
    for x in df["due_date_iso"].dropna().astype(str):
        d = days_until(x)
        if d is not None and 0 <= d <= 21:
            due_soon += 1

    return {
        "run_id": run_id,
        "total_listings": int(len(df)),
        "successful_sources": int(sdf["success"].sum()) if not sdf.empty else 0,
        "failed_sources": int((sdf["success"] == 0).sum()) if not sdf.empty else 0,
        "score_stats": {
            "min": int(df.score.min()),
            "max": int(df.score.max()),
            "average": round(float(df.score.mean()), 2),
            "median": round(float(df.score.median()), 2),
        },
        "score_band_counts": df.score_band.value_counts().to_dict(),
        "action_counts": df.action_level.fillna("Unknown").value_counts().to_dict(),
        "county_counts": df.county.fillna("Unknown").value_counts().to_dict(),
        "municipality_counts": df.municipality.fillna("Unknown").value_counts().to_dict(),
        "trade_counts": df.trade_category.fillna("Unknown").value_counts().to_dict(),
        "tier_counts": df.trade_tier.fillna("Unmatched").value_counts().to_dict(),
        "source_counts": df.source_name.value_counts().to_dict(),
        "due_soon_21_days": due_soon,
        "top_15_by_score": df.head(15)[[
            "score", "score_band", "action_level", "confidence_score", "title", "source_name",
            "county", "municipality", "trade_category", "trade_tier", "due_date_iso", "detail_url"
        ]].to_dict(orient="records"),
    }


def esc(x: Any) -> str:
    return "" if x is None else html.escape(str(x))


def report_html(df: pd.DataFrame, sdf: pd.DataFrame, summ: Dict[str, Any], run_id: str) -> str:
    bands = summ.get("score_band_counts", {})
    stats = summ.get("score_stats", {})
    action_counts = summ.get("action_counts", {})

    rows: List[str] = []
    if df.empty:
        rows.append('<tr><td colspan="13">No listings saved.</td></tr>')

    for rank, (_, r) in enumerate(df.iterrows(), start=1):
        reasons = explode_json_list(r.get("score_reasons_json", "")).split("; ") if r.get("score_reasons_json", "") else []
        docs = explode_json_list(r.get("linked_documents_json", "")).split("; ") if r.get("linked_documents_json", "") else []
        matched = explode_json_list(r.get("matched_trades_json", ""))

        reason_html = "<br>".join(esc(x) for x in reasons[:8])
        docs_html = "<br>".join(f'<a href="{esc(u)}" target="_blank">document</a>' for u in docs[:4] if u)

        rows.append(f"""
        <tr>
            <td>{rank}</td>
            <td><span class="band {esc(r.score_band)}">{esc(r.score_band)}</span><br><b>{esc(r.score)}</b></td>
            <td>{esc(r.action_level)}<br><span class="muted">Confidence: {esc(r.confidence_score)}</span></td>
            <td><a href="{esc(r.detail_url)}" target="_blank">{esc(r.title)}</a><br><span class="muted">{esc(r.source_name)}</span></td>
            <td>{esc(r.county)}<br><span class="muted">{esc(r.municipality)}</span></td>
            <td>{esc(r.trade_category)}<br><span class="muted">{esc(r.trade_tier)}</span><br><span class="muted">{esc(matched)}</span></td>
            <td>{esc(r.status)}</td>
            <td>{esc(r.due_date_iso)}<br><span class="muted">Pre-bid: {esc(r.prebid_date_iso)}</span></td>
            <td>{esc(r.contact_info)}</td>
            <td>{esc(r.document_count)}<br>{docs_html}</td>
            <td>{esc(r.summary)}</td>
            <td class="reasons">{reason_html}</td>
        </tr>
        """)

    srows: List[str] = []
    for _, r in sdf.iterrows():
        stat = "Success" if int(r.success) == 1 else "Failed"
        srows.append(
            f"<tr><td>{esc(stat)}</td><td><a href='{esc(r.url)}' target='_blank'>{esc(r.source_name)}</a></td><td>{esc(r.county)}</td><td>{esc(r.municipality)}</td><td>{esc(r.source_type)}</td><td>{esc(r.candidates_found)}</td><td>{esc(r.error_message)}</td><td>{esc(r.checked_at)}</td></tr>"
        )

    county_items = "".join(f"<li>{esc(k)}: {esc(v)}</li>" for k, v in summ.get("county_counts", {}).items())
    mun_items = "".join(f"<li>{esc(k)}: {esc(v)}</li>" for k, v in summ.get("municipality_counts", {}).items())
    trade_items = "".join(f"<li>{esc(k)}: {esc(v)}</li>" for k, v in summ.get("trade_counts", {}).items())
    action_items = "".join(f"<li>{esc(k)}: {esc(v)}</li>" for k, v in action_counts.items())

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>NJ Bid Intelligence Report {esc(run_id)}</title>
<style>
body{{font-family:Arial,sans-serif;margin:24px;background:#f6f7fb;color:#1f2937}}
h1,h2{{margin-bottom:8px}}
.muted{{color:#667085;font-size:.9em}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin:18px 0}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
.big{{font-size:1.8em;font-weight:bold}}
table{{width:100%;border-collapse:collapse;margin-top:16px;background:#fff}}
th,td{{border:1px solid #e5e7eb;padding:8px;vertical-align:top;font-size:.9em}}
th{{background:#eef2f7;text-align:left;position:sticky;top:0}}
.band{{display:inline-block;padding:3px 7px;border-radius:999px;font-size:.8em;font-weight:bold;background:#eee}}
.HOT{{background:#d1fae5;color:#065f46}}
.REVIEW{{background:#fef3c7;color:#92400e}}
.MONITOR{{background:#dbeafe;color:#1e40af}}
.ARCHIVE{{background:#e5e7eb;color:#374151}}
.reasons{{font-size:.83em;color:#475467}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px}}
</style>
</head>
<body>
<h1>NJ Public Bid Intelligence Report</h1>
<p class="muted">Run ID: {esc(run_id)} | Generated: {esc(dt.datetime.now().strftime('%Y-%m-%d %I:%M %p'))} | County + municipality source coverage</p>

<div class="cards">
  <div class="card"><div class="muted">Total Listings</div><div class="big">{esc(summ.get('total_listings',0))}</div></div>
  <div class="card"><div class="muted">HOT</div><div class="big">{esc(bands.get('HOT',0))}</div></div>
  <div class="card"><div class="muted">REVIEW</div><div class="big">{esc(bands.get('REVIEW',0))}</div></div>
  <div class="card"><div class="muted">Due Soon ≤21 Days</div><div class="big">{esc(summ.get('due_soon_21_days',0))}</div></div>
  <div class="card"><div class="muted">Successful Sources</div><div class="big">{esc(summ.get('successful_sources',0))}</div></div>
</div>

<div class="grid">
  <div class="card"><h2>Score Comparison</h2><p>Max: <b>{esc(stats.get('max',''))}</b></p><p>Average: <b>{esc(stats.get('average',''))}</b></p><p>Median: <b>{esc(stats.get('median',''))}</b></p><p>Min: <b>{esc(stats.get('min',''))}</b></p></div>
  <div class="card"><h2>By County</h2><ul>{county_items}</ul></div>
  <div class="card"><h2>By Municipality</h2><ul>{mun_items}</ul></div>
  <div class="card"><h2>By Trade</h2><ul>{trade_items}</ul></div>
  <div class="card"><h2>By Action</h2><ul>{action_items}</ul></div>
</div>

<h2>Ranked Bid Listings</h2>
<table>
<thead><tr><th>Rank</th><th>Score</th><th>Action</th><th>Project / Source</th><th>Area</th><th>Trade Match</th><th>Status</th><th>Dates</th><th>Contact</th><th>Docs</th><th>Summary</th><th>Score Reasons</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>

<h2>Source Run Details</h2>
<table>
<thead><tr><th>Status</th><th>Source</th><th>County</th><th>Municipality</th><th>Type</th><th>Candidates</th><th>Error</th><th>Checked</th></tr></thead>
<tbody>{''.join(srows)}</tbody>
</table>
</body>
</html>"""


def build_coverage_report(sources: List[Source], areas_path: Path, run_id: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    area_cfg = load_yaml(areas_path) if areas_path.exists() else {}
    counties = area_cfg.get("counties", {})

    configured = {(s.county.strip().lower(), s.municipality.strip().lower()) for s in sources if s.municipality.strip()}

    for county, municipalities in counties.items():
        for mun in municipalities:
            has_source = (county.lower(), str(mun).lower()) in configured
            matching_sources = [s.name for s in sources if s.county.lower() == county.lower() and s.municipality.lower() == str(mun).lower()]
            rows.append({
                "county": county,
                "municipality": mun,
                "has_configured_source": has_source,
                "sources": "; ".join(matching_sources),
                "recommended_action": "OK" if has_source else "Add municipal purchasing/bids URL to config/sources.yaml",
            })

    # Also include configured sources not in areas map.
    for s in sources:
        if s.municipality and (s.county.lower(), s.municipality.lower()) not in configured:
            rows.append({
                "county": s.county,
                "municipality": s.municipality,
                "has_configured_source": True,
                "sources": s.name,
                "recommended_action": "Configured",
            })

    path = out_dir / f"coverage_report_{run_id}.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def write_reports(conn: sqlite3.Connection, run_id: str, out_dir: Path, sources: List[Source], areas_path: Path) -> Tuple[Path, Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    df = df_for_run(conn, run_id)
    sdf = sources_for_run(conn, run_id)
    summ = summary(df, sdf, run_id)

    csv_path = out_dir / f"bid_report_{run_id}.csv"
    html_path = out_dir / f"bid_report_{run_id}.html"
    json_path = out_dir / f"run_summary_{run_id}.json"
    coverage_path = build_coverage_report(sources, areas_path, run_id, out_dir)

    export = df.copy()
    if not export.empty:
        export["matched_trades"] = export["matched_trades_json"].apply(explode_json_list)
        export["score_reasons"] = export["score_reasons_json"].apply(explode_json_list)
        export["missing_fields"] = export["missing_fields_json"].apply(explode_json_list)
        export["linked_documents"] = export["linked_documents_json"].apply(explode_json_list)
        export = export.drop(columns=["matched_trades_json", "score_reasons_json", "missing_fields_json", "linked_documents_json"])

    export.to_csv(csv_path, index=False)
    html_path.write_text(report_html(df, sdf, summ, run_id), encoding="utf-8")
    json_path.write_text(json.dumps(summ, indent=2), encoding="utf-8")
    return csv_path, html_path, json_path, coverage_path


def finish_run(conn: sqlite3.Connection, run_id: str, ok: int, failed: int, candidates: int, saved: int, dupes: int, csv_path: Path, html_path: Path, json_path: Path, coverage_path: Path) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?,sources_successful=?,sources_failed=?,candidates_found=?,listings_saved=?,duplicates_skipped=?,report_csv=?,report_html=?,report_json=?,coverage_csv=? WHERE run_id=?",
        (now_utc(), ok, failed, candidates, saved, dupes, str(csv_path), str(html_path), str(json_path), str(coverage_path), run_id),
    )
    conn.commit()


def run(
    config: Path,
    trade_rules: Path,
    areas: Path,
    db: Path,
    out: Path,
    logs: Path,
    counties: List[str],
    municipalities: List[str],
    source_types: List[str],
    include_statewide: bool,
    max_sources: Optional[int],
) -> None:
    run_id = run_stamp()
    log_path = setup_logging(logs, run_id)

    settings, all_sources = load_sources(config)
    rules = load_rules(trade_rules)

    targets = counties or settings.get("target_counties", [])
    target_muns = municipalities or settings.get("target_municipalities", [])
    delay = float(settings.get("request_delay_seconds", 0.8))
    timeout = int(settings.get("timeout_seconds", 30))
    max_detail = int(settings.get("max_detail_pages_per_source", 12))
    same_domain_only = bool(settings.get("same_domain_only", True))

    sources = filter_sources(all_sources, targets, target_muns, source_types, include_statewide)
    if max_sources is not None:
        sources = sources[:max_sources]

    conn = init_db(db)
    start_run(conn, run_id, len(sources), log_path)

    logging.info("Started run %s", run_id)
    logging.info("Total active sources loaded: %s", len(all_sources))
    logging.info("Sources selected for this run: %s", len(sources))
    logging.info("Target counties: %s", ", ".join(targets) if targets else "all")
    logging.info("Target municipalities: %s", ", ".join(target_muns) if target_muns else "all configured")

    s = make_session()

    ok = failed = candidates_total = saved = dupes = 0

    for i, source in enumerate(sources, 1):
        logging.info("[%s/%s] Scraping %s", i, len(sources), source.name)
        try:
            candidates = scrape_source(s, source, timeout, max_detail, rules, same_domain_only)
            candidates_total += len(candidates)
            ok += 1
            source_result(conn, run_id, source, True, len(candidates))
            logging.info("Found %s candidates from %s", len(candidates), source.name)

            for cand in candidates:
                listing = normalize_candidate(cand, run_id, targets, target_muns, rules)
                if store_listing(conn, listing):
                    saved += 1
                    logging.info("Saved score=%s action=%s title=%s", listing.score, listing.action_level, listing.title)
                else:
                    dupes += 1
                    logging.info("Duplicate within run skipped title=%s", listing.title)

        except Exception as e:
            failed += 1
            source_result(conn, run_id, source, False, 0, f"{type(e).__name__}: {e}")
            logging.exception("Source failed: %s", source.name)

        time.sleep(delay)

    csv_path, html_path, json_path, coverage_path = write_reports(conn, run_id, out, all_sources, areas)
    finish_run(conn, run_id, ok, failed, candidates_total, saved, dupes, csv_path, html_path, json_path, coverage_path)

    print("\nDONE")
    print(f"Run ID:              {run_id}")
    print(f"Sources selected:    {len(sources)}")
    print(f"Sources successful:  {ok}")
    print(f"Sources failed:      {failed}")
    print(f"Candidates found:    {candidates_total}")
    print(f"Listings saved:      {saved}")
    print(f"Duplicates skipped:  {dupes}")
    print(f"CSV report:          {csv_path}")
    print(f"HTML report:         {html_path}")
    print(f"JSON summary:        {json_path}")
    print(f"Coverage report:     {coverage_path}")
    print(f"Log file:            {log_path}")


def regenerate(db: Path, out: Path, config: Path, areas: Path, run_id: Optional[str], latest: bool) -> None:
    conn = init_db(db)
    if latest:
        run_id = latest_run_id(conn)
    if not run_id:
        raise SystemExit("No run_id supplied and no latest run found.")
    _, sources = load_sources(config)
    csv_path, html_path, json_path, coverage_path = write_reports(conn, run_id, out, sources, areas)
    print(f"Report regenerated for {run_id}")
    print(f"CSV:      {csv_path}")
    print(f"HTML:     {html_path}")
    print(f"JSON:     {json_path}")
    print(f"Coverage: {coverage_path}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="NJ bid intelligence scraper for state, county, and municipal bid sources.")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--config", type=Path, default=Path("config/sources.yaml"))
    r.add_argument("--trade-rules", type=Path, default=Path("config/trade_rules.yaml"))
    r.add_argument("--areas", type=Path, default=Path("config/areas.yaml"))
    r.add_argument("--db", type=Path, default=Path("data/bids.sqlite"))
    r.add_argument("--out", type=Path, default=Path("output"))
    r.add_argument("--logs", type=Path, default=Path("logs"))
    r.add_argument("--counties", default="", help="Comma-separated county filter, e.g. Bergen,Essex")
    r.add_argument("--municipalities", default="", help="Comma-separated municipality filter, e.g. Newark,Clifton")
    r.add_argument("--source-types", default="", help="Comma-separated source type filter, e.g. municipality,county,state")
    r.add_argument("--include-statewide", action="store_true", help="Include statewide/county sources when filtering municipalities.")
    r.add_argument("--max-sources", type=int, default=None, help="Optional limit for testing.")

    rep = sub.add_parser("report")
    rep.add_argument("--db", type=Path, default=Path("data/bids.sqlite"))
    rep.add_argument("--out", type=Path, default=Path("output"))
    rep.add_argument("--config", type=Path, default=Path("config/sources.yaml"))
    rep.add_argument("--areas", type=Path, default=Path("config/areas.yaml"))
    rep.add_argument("--run-id")
    rep.add_argument("--latest-run", action="store_true")

    a = p.parse_args(argv)

    if a.cmd == "run":
        run(
            config=a.config,
            trade_rules=a.trade_rules,
            areas=a.areas,
            db=a.db,
            out=a.out,
            logs=a.logs,
            counties=parse_csv_arg(a.counties),
            municipalities=parse_csv_arg(a.municipalities),
            source_types=parse_csv_arg(a.source_types),
            include_statewide=a.include_statewide,
            max_sources=a.max_sources,
        )
    elif a.cmd == "report":
        regenerate(a.db, a.out, a.config, a.areas, a.run_id, a.latest_run)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
