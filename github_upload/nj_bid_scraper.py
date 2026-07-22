#!/usr/bin/env python3
from __future__ import annotations

import argparse, dataclasses, datetime as dt, hashlib, html, json, logging, re, sqlite3, sys, time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

try:
    import fitz
except Exception:
    fitz = None

USER_AGENT = "NJPublicBidScraper-NoAI/1.0 contact=your-email@example.com"

BID_WORDS = [
    # Core public-bid language.
    "bid", "bids", "bidder", "bidders", "bidding", "bid package", "bid packet",
    "bid documents", "bid doc", "bid opening", "bid due", "bid deadline", "bid date",
    "notice to bidders", "notice inviting bids", "invitation to bid", "invitation for bids",
    "ifb", "sealed bid", "sealed bids", "sealed proposal", "sealed proposals",
    "public bid", "public bidding", "public works bid", "competitive contracting",

    # RFP/RFQ/procurement terms that still commonly contain construction opportunities.
    "rfp", "request for proposal", "request for proposals", "rfq", "request for qualifications",
    "quote", "quotes", "quotation", "proposal", "proposals", "submittal", "submission",
    "solicitation", "opportunity", "procurement", "purchasing", "contract", "contracts",
    "contractor", "contractors", "prime contractor", "general contractor", "public works",
    "prevailing wage", "certified payroll", "bid bond", "performance bond", "payment bond",
    "maintenance bond", "surety", "addendum", "addenda", "specification", "specifications",
    "specs", "plans", "drawings", "project manual", "construction documents", "scope of work",
    "bid form", "proposal form", "legal advertisement", "advertisement", "vendor", "planholder",
]

# These are no longer general trade categories. They are discovery words that help the
# scraper keep construction-looking listings long enough to classify them by your target trades.
# Arbitrary trades such as roadwork, milling, HVAC, electrical, and plumbing were intentionally removed.
CONSTRUCTION_WORDS = [
    "construction", "renovation", "renovations", "alteration", "alterations", "improvement",
    "improvements", "rehabilitation", "restoration", "repair", "repairs", "replacement",
    "building", "facility", "public works", "contractor", "construction services",
    "construction project", "capital improvement", "capital project", "dpmc", "division 1",
    "division 3", "division 4", "division 7", "general construction", "sitework", "site work",
    "concrete", "roof", "roofing", "addition", "building addition", "masonry", "waterproof",
    "waterproofing", "structural", "interior", "exterior", "facade", "façade",
]

# Terms that indicate useful paperwork or important bid-supporting information.
DOCUMENT_WORDS = [
    "plans", "drawings", "specifications", "specification", "specs", "project manual",
    "bid documents", "bid package", "bid packet", "contract documents", "construction documents",
    "proposal form", "bid form", "form of bid", "addendum", "addenda", "plan holder",
    "planholder", "document", "documents", "download", "attachment", "attachments",
    "forms", "legal notice", "notice to bidders", "scope of work", "instructions to bidders",
]

# Expanded negative language. These listings are usually closed, stale, irrelevant, or already decided.
NEGATIVE_WORDS = [
    "canceled", "cancelled", "cancellation", "closed", "expired", "archive", "archived",
    "awarded", "award pending", "recommendation of award", "intent to award", "bid results",
    "results", "tabulation", "bid tab", "bid tabs", "no longer accepting", "not accepting",
    "submission period closed", "deadline has passed", "has been awarded", "postponed indefinitely",
    "withdrawn", "rejected", "all bids rejected", "rebid pending", "cancelled solicitation",
    "closed bids", "past bids", "past opportunities", "inactive", "not available", "for information only",
]

# Positive status phrases help identify opportunities that are still live.
OPEN_WORDS = [
    "open", "currently open", "active", "accepting bids", "accepting proposals", "now accepting",
    "available", "current bid", "current bids", "current opportunity", "currently advertised",
    "advertised", "out to bid", "open for bidding", "bids wanted", "proposals wanted",
]

# Targeted trade list only. Removed arbitrary categories such as roadwork, milling, HVAC,
# electrical, plumbing, parks, landscape, and other non-priority trades.
TRADE_KEYWORDS = {
    "Tuck Pointing": [
        "tuck pointing", "tuck-pointing", "tuckpointing", "repointing", "re-pointing",
        "pointing", "brick pointing", "masonry pointing", "stone pointing", "joint repointing",
        "mortar joint", "mortar joints", "mortar repair", "mortar repairs", "mortar replacement",
        "mortar restoration", "rake and repoint", "raking and repointing", "grind and point",
        "grinding and pointing", "remove and repoint", "repoint brick", "repoint masonry",
    ],
    "Masonry": [
        "masonry", "masonry restoration", "masonry repair", "masonry repairs", "masonry rehabilitation",
        "masonry preservation", "brick", "brickwork", "brick restoration", "brick repair", "brick repairs",
        "brick replacement", "brick reconstruction", "stone", "stonework", "stone restoration",
        "stone repair", "stone repairs", "terra cotta", "terra-cotta", "terracotta", "terra cotta repair",
        "terra cotta restoration", "block", "cmu", "concrete masonry", "masonry wall", "brick wall",
        "stone wall", "parapet", "parapet repair", "parapet reconstruction", "chimney repair",
        "lintel", "lintel repair", "lintel replacement", "sill replacement", "coping stone",
        "through-wall flashing", "masonry cleaning", "masonry stabilization",
    ],
    "Waterproofing": [
        "waterproof", "waterproofing", "water proof", "water proofing", "below grade waterproofing",
        "below-grade waterproofing", "foundation waterproofing", "foundation wall waterproofing",
        "dampproofing", "damp proofing", "damp-proofing", "water infiltration", "leak repair",
        "leak repairs", "injection waterproofing", "crack injection", "epoxy injection", "polyurethane injection",
        "urethane injection", "negative side waterproofing", "positive side waterproofing", "blindside waterproofing",
        "waterproofing membrane", "fluid applied waterproofing", "liquid applied waterproofing",
        "elastomeric waterproofing", "traffic coating", "deck coating", "plaza deck", "joint sealant",
        "joint sealants", "sealant replacement", "sealants", "caulking", "backer rod", "expansion joint",
        "building envelope waterproofing", "air barrier", "vapor barrier", "weather barrier",
    ],
    "Roof Restoration": [
        "roof restoration", "roof coating", "roof coatings", "roof coating system", "silicone roof coating",
        "silicone roof restoration", "silicone coating", "silicone membrane", "fluid applied roof",
        "fluid-applied roof", "liquid applied roof", "liquid-applied roof", "elastomeric roof coating",
        "reflective roof coating", "roof recover", "recover roof", "roof rehabilitation", "roof repair",
        "roof repairs", "roof maintenance", "roof membrane restoration", "membrane restoration",
        "roof restoration system", "epdm restoration", "tpo restoration", "modified bitumen restoration",
        "flashing repair", "flashing replacement", "roof patching", "roof resealing",
    ],
    "Structural Reinforcement": [
        "structural reinforcement", "structural reinforcing", "structural strengthening", "strengthening",
        "frp", "frp reinforcement", "fiber reinforced polymer", "fibre reinforced polymer", "carbon fiber",
        "carbon fibre", "carbon fiber reinforcement", "cfrp", "glass fiber reinforcement", "gfrp",
        "steel reinforcement", "steel reinforcing", "beam reinforcement", "beam strengthening",
        "column reinforcement", "column strengthening", "slab reinforcement", "slab strengthening",
        "wall reinforcement", "wall strengthening", "structural steel reinforcement", "sistering",
        "supplemental steel", "supplemental framing", "anchor reinforcement", "tie back", "tie-back",
        "helical pier", "helical pile", "structural stabilization", "stabilization work",
    ],
    "Structural Alterations": [
        "structural alteration", "structural alterations", "structural modification", "structural modifications",
        "structural repair", "structural repairs", "structural remediation", "structural renovation",
        "load bearing", "load-bearing", "bearing wall", "bearing walls", "remove bearing wall",
        "new opening", "new openings", "wall opening", "masonry opening", "beam replacement",
        "column replacement", "beam installation", "new beam", "new lintel", "steel lintel",
        "shoring", "temporary shoring", "underpinning", "foundation underpinning", "foundation alteration",
        "foundation repair", "structural steel", "miscellaneous steel", "steel framing", "framing alteration",
        "floor opening", "roof opening", "structural demolition", "selective structural demolition",
    ],
    "Interior Renovations": [
        "interior renovation", "interior renovations", "renovate interior", "renovation of interior",
        "building renovation", "facility renovation", "office renovation", "classroom renovation",
        "corridor renovation", "lobby renovation", "bathroom renovation", "toilet room renovation",
        "tenant improvement", "tenant improvements", "tenant fit out", "tenant fit-out", "fit out", "fit-out",
        "build out", "build-out", "interior fit out", "interior fit-out", "interior construction",
        "interior finishes", "finish renovation", "drywall", "gypsum board", "partition", "partitions",
        "doors frames hardware", "flooring replacement", "ceiling replacement", "paint and finishes",
    ],
    "Interior Alterations": [
        "interior alteration", "interior alterations", "alter interior", "alterations to interior",
        "space reconfiguration", "reconfigure space", "interior reconfiguration", "room reconfiguration",
        "partition alterations", "partition work", "new partitions", "wall relocation", "selective demolition",
        "interior demolition", "demolition and renovation", "interior improvements", "office alterations",
        "classroom alterations", "building alterations", "facility alterations", "minor alterations",
        "general alterations", "renovations and alterations",
    ],
    "Building/General Construction": [
        "building construction", "general construction", "building improvements", "facility improvements",
        "capital improvements", "construction services", "sitework", "site work", "site improvements",
        "excavation", "grading", "earthwork", "foundation", "foundations", "concrete", "concrete work",
        "concrete repair", "concrete repairs", "concrete restoration", "slab", "slabs", "sidewalk concrete",
        "cast-in-place concrete", "roofing", "roof replacement", "roofing replacement", "new roof",
        "roof membrane", "roof system", "addition", "building addition", "new addition", "addition and renovation",
        "addition and alterations", "building expansion", "facility addition", "building envelope", "exterior envelope",
    ],
}

HIGH_MARGIN_TRADES = {
    "Masonry", "Waterproofing", "Tuck Pointing", "Roof Restoration",
    "Structural Reinforcement", "Structural Alterations",
}

INTERIOR_TRADES = {"Interior Renovations", "Interior Alterations"}

BROAD_TARGET_TRADES = {"Building/General Construction"}

TARGET_TRADES = HIGH_MARGIN_TRADES | INTERIOR_TRADES | BROAD_TARGET_TRADES

TRADE_PRIORITY_BOOSTS = {
    "Primary - Priority Trade": 38,
    "Secondary - Interior Trade": 26,
    "Broad - General Construction": 15,
    "Unclassified": 0,
}

def trade_priority(trade: Optional[str]) -> str:
    if trade in HIGH_MARGIN_TRADES:
        return "Primary - Priority Trade"
    if trade in INTERIOR_TRADES:
        return "Secondary - Interior Trade"
    if trade in BROAD_TARGET_TRADES:
        return "Broad - General Construction"
    return "Unclassified"

DATE_PATTERNS = [
    r"(?:bid(?:s)?|proposal(?:s)?|quote(?:s)?|submission(?:s)?|responses?)\s*(?:are\s*)?(?:due|received|opened|opening|close|closing|deadline)?\s*(?:date|on|by|at|:)?\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}(?:\s+(?:at\s*)?\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(?:bid(?:s)?|proposal(?:s)?|quote(?:s)?|submission(?:s)?|responses?)\s*(?:are\s*)?(?:due|received|opened|opening|close|closing|deadline)?\s*(?:date|on|by|at|:)?\s*(\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(?:closing|opening|due|deadline)\s*(?:date|on|by|at|:)?\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}(?:\s+(?:at\s*)?\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(?:closing|opening|due|deadline)\s*(?:date|on|by|at|:)?\s*(\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))",
]
PREBID_PATTERNS = [
    r"(?:mandatory\s*)?(?:pre[-\s]?bid|prebid|site visit|walkthrough|walk-through).{0,100}?([A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}(?:\s+(?:at\s*)?\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
    r"(?:mandatory\s*)?(?:pre[-\s]?bid|prebid|site visit|walkthrough|walk-through).{0,100}?(\d{1,2}/\d{1,2}/\d{2,4}(?:\s+\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?)?)",
]
EMAIL_PATTERN = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
PHONE_PATTERN = r"(?:\(\d{3}\)\s*|\d{3}[-.\s])\d{3}[-.\s]\d{4}"

@dataclasses.dataclass
class Source:
    id: str; name: str; county: str; url: str; source_type: str; scraper_type: str; active: bool = True

@dataclasses.dataclass
class Candidate:
    source_id: str; source_name: str; source_county: str; source_url: str; source_type: str; scraper_type: str
    title: str; detail_url: str; raw_text: str; linked_documents: List[str]

@dataclasses.dataclass
class Listing:
    run_id: str; source_id: str; source_name: str; source_county: str; source_url: str; source_type: str; scraper_type: str
    title: str; detail_url: str; county: Optional[str]; trade_category: Optional[str]; trade_priority: str; status: Optional[str]
    due_date_raw: Optional[str]; due_date_iso: Optional[str]; prebid_date_raw: Optional[str]; prebid_date_iso: Optional[str]
    summary: str; contact_info: str; linked_documents: List[str]; construction_relevance: int; score: int; score_band: str
    score_reasons: List[str]; raw_text: str; raw_hash: str; dedupe_key: str; scraped_at: str

def now_utc() -> str: return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
def run_stamp() -> str: return dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
def normalize_ws(text: str) -> str: return re.sub(r"\s+", " ", text or "").strip()
def truncate(text: str, limit: int = 500) -> str:
    text = normalize_ws(text); return text if len(text) <= limit else text[:limit].rstrip() + "..."
def sha(text: str) -> str: return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
def contains_any(text: str, words: Iterable[str]) -> bool:
    lower = text.lower(); return any(w.lower() in lower for w in words)
def count_matches(text: str, words: Iterable[str]) -> int:
    lower = text.lower(); return sum(1 for w in words if w.lower() in lower)
def looks_like_bid(text: str) -> bool:
    text = normalize_ws(text); return len(text) >= 12 and (contains_any(text, BID_WORDS) or contains_any(text, CONSTRUCTION_WORDS))
def domain(url: str) -> str: return urlparse(url).netloc.lower().replace("www.", "")
def same_domain(a: str, b: str) -> bool: return domain(a) == domain(b)
def is_pdf(url: str, ctype: str = "") -> bool: return ctype == "application/pdf" or urlparse(url).path.lower().endswith(".pdf")

def load_config(path: Path) -> Tuple[Dict[str, Any], List[Source]]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings = cfg.get("settings", {})
    sources = []
    for row in cfg.get("sources", []):
        s = Source(
            id=str(row["id"]), name=str(row["name"]), county=str(row.get("county", "")), url=str(row["url"]),
            source_type=str(row.get("source_type", "")), scraper_type=str(row.get("scraper_type", "generic_html")), active=bool(row.get("active", True))
        )
        if s.active: sources.append(s)
    return settings, sources

def setup_logging(log_dir: Path, run_id: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True); log_path = log_dir / f"scraper_{run_id}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)])
    return log_path

def session() -> requests.Session:
    s = requests.Session(); s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,*/*"}); return s

def fetch(s: requests.Session, url: str, timeout: int) -> Tuple[bytes, str, str]:
    logging.info("Fetching %s", url); r = s.get(url, timeout=timeout, allow_redirects=True); r.raise_for_status()
    return r.content, r.headers.get("content-type", "").split(";")[0].lower(), r.url

def soup_from(content: bytes) -> BeautifulSoup:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]): tag.decompose()
    return soup

def pdf_text(content: bytes) -> str:
    if fitz is None: return ""
    parts = []
    with fitz.open(stream=content, filetype="pdf") as doc:
        for page in doc: parts.append(page.get_text("text"))
    return normalize_ws(" ".join(parts))

def extract_links(soup: BeautifulSoup, base_url: str) -> List[Tuple[str, str]]:
    out = []
    for a in soup.find_all("a", href=True):
        text = normalize_ws(a.get_text(" ", strip=True)); url = urljoin(base_url, a.get("href", ""))
        if url.startswith(("http://", "https://")): out.append((text, url))
    return out

def link_useful(text: str, url: str) -> bool:
    c = f"{text} {url}".lower()
    return any(w.lower() in c for w in BID_WORDS + CONSTRUCTION_WORDS + DOCUMENT_WORDS + [".pdf", ".doc", ".docx", "download", "detail"])

def blocks(soup: BeautifulSoup) -> List[Tuple[str, Optional[str], List[str]]]:
    rows = []
    for tag_name in ["tr", "li", "article", "section", "div"]:
        for block in soup.find_all(tag_name, limit=500):
            text = normalize_ws(block.get_text(" ", strip=True))
            if len(text) < 20: continue
            hrefs = [a.get("href", "") for a in block.find_all("a", href=True)]
            rows.append((text, hrefs[0] if hrefs else None, hrefs))
    seen, unique = set(), []
    for text, first, hrefs in rows:
        key = sha(text[:1000])
        if key not in seen:
            seen.add(key); unique.append((text, first, hrefs))
    return unique

def derive_title(text: str) -> str:
    text = normalize_ws(text)
    for pattern in [r"Bid Title\s*[:\-]\s*(.{10,160})", r"Project\s*(?:Name|Title)?\s*[:\-]\s*(.{10,160})", r"Title\s*[:\-]\s*(.{10,160})", r"Notice to Bidders\s*[:\-]?\s*(.{10,160})"]:
        m = re.search(pattern, text, flags=re.I)
        if m: return truncate(m.group(1), 140)
    for chunk in re.split(r"(?<=[.!?])\s+|\s{3,}|\|", text[:1500]):
        chunk = normalize_ws(chunk)
        if 15 <= len(chunk) <= 160 and looks_like_bid(chunk): return truncate(chunk, 140)
    return truncate(text, 140)

def enrich_detail(s: requests.Session, base_url: str, detail_url: str, timeout: int) -> Tuple[str, List[str]]:
    try:
        if not same_domain(base_url, detail_url) and not detail_url.lower().endswith(".pdf"): return "", []
        content, ctype, final = fetch(s, detail_url, timeout)
        if is_pdf(final, ctype): return pdf_text(content), [final]
        sp = soup_from(content); text = normalize_ws(sp.get_text(" ", strip=True))
        docs = [u for t,u in extract_links(sp, final) if link_useful(t,u)]
        return text, docs[:20]
    except Exception as e:
        logging.warning("Could not enrich %s: %s", detail_url, e); return "", []

def scrape_generic(s: requests.Session, source: Source, timeout: int, max_detail: int) -> List[Candidate]:
    content, ctype, final = fetch(s, source.url, timeout)
    if is_pdf(final, ctype):
        text = pdf_text(content)
        return [Candidate(source.id, source.name, source.county, source.url, source.source_type, source.scraper_type, source.name, final, text, [final])] if looks_like_bid(text) else []
    sp = soup_from(content); out = []
    page_text = normalize_ws(sp.get_text(" ", strip=True))
    page_docs = [u for t,u in extract_links(sp, final) if link_useful(t,u)]
    if looks_like_bid(page_text): out.append(Candidate(source.id, source.name, source.county, source.url, source.source_type, source.scraper_type, source.name, final, page_text, page_docs[:20]))
    enriched = 0
    for text, first, hrefs in blocks(sp):
        if not looks_like_bid(text): continue
        links = [urljoin(final, h) for h in hrefs]
        detail = urljoin(final, first) if first else final
        extra, docs = "", []
        if detail != final and enriched < max_detail:
            extra, docs = enrich_detail(s, final, detail, timeout); enriched += 1
        full = normalize_ws(f"{text} {extra}")
        out.append(Candidate(source.id, source.name, source.county, source.url, source.source_type, source.scraper_type, derive_title(full), detail, full, list(dict.fromkeys(links + docs))[:30]))
    return out

def scrape_civic(s: requests.Session, source: Source, timeout: int, max_detail: int) -> List[Candidate]:
    content, ctype, final = fetch(s, source.url, timeout); sp = soup_from(content); out = []; enriched = 0
    for text, first, hrefs in blocks(sp):
        lower = text.lower()
        civic_hint = any(h in lower for h in ["bid title", "category", "status", "description", "publication date", "closing date", "submittal information"])
        if not (civic_hint or looks_like_bid(text)): continue
        links = [urljoin(final, h) for h in hrefs]; detail = urljoin(final, first) if first else final
        extra, docs = "", []
        if detail != final and enriched < max_detail:
            extra, docs = enrich_detail(s, final, detail, timeout); enriched += 1
        full = normalize_ws(f"{text} {extra}")
        if looks_like_bid(full): out.append(Candidate(source.id, source.name, source.county, source.url, source.source_type, source.scraper_type, derive_title(full), detail, full, list(dict.fromkeys(links + docs))[:30]))
    return out

def scrape_pdf_direct(s: requests.Session, source: Source, timeout: int, max_detail: int) -> List[Candidate]:
    content, ctype, final = fetch(s, source.url, timeout)
    text = pdf_text(content) if is_pdf(final, ctype) else normalize_ws(soup_from(content).get_text(" ", strip=True))
    return [Candidate(source.id, source.name, source.county, source.url, source.source_type, source.scraper_type, derive_title(text) or source.name, final, text, [final])] if looks_like_bid(text) else []

def scrape_source(s: requests.Session, source: Source, timeout: int, max_detail: int) -> List[Candidate]:
    if source.scraper_type == "civicengage": return scrape_civic(s, source, timeout, max_detail)
    if source.scraper_type == "pdf_direct": return scrape_pdf_direct(s, source, timeout, max_detail)
    if source.scraper_type == "generic_html": return scrape_generic(s, source, timeout, max_detail)
    raise ValueError(f"Unknown scraper_type: {source.scraper_type}")

def parse_date(text: str, patterns: List[str]) -> Tuple[Optional[str], Optional[str]]:
    for p in patterns:
        m = re.search(p, text, flags=re.I|re.S)
        if m:
            raw = normalize_ws(m.group(1)).replace(" at ", " ")
            try: return raw, date_parser.parse(raw, fuzzy=True).isoformat(timespec="minutes")
            except Exception: pass
    return None, None

def infer_county(text: str, source_county: str, targets: List[str]) -> Optional[str]:
    if source_county and source_county.lower() != "statewide": return source_county
    lower = text.lower()
    for c in targets:
        if re.search(rf"\b{re.escape(c.lower())}\b", lower): return c
    return None

def infer_trade(text: str) -> Tuple[Optional[str], int]:
    lower = text.lower(); scored = []
    for trade, words in TRADE_KEYWORDS.items():
        score = 0
        for w in words:
            wl = w.lower()
            if wl in lower:
                # Multi-word trade phrases are stronger than single generic words.
                score += 4 if " " in wl or "-" in wl else 1
        if score:
            # Specialized trades win over broad general-construction matches when scores are close.
            priority = trade_priority(trade)
            if priority == "Primary - Priority Trade": score += 10
            elif priority == "Secondary - Interior Trade": score += 6
            elif priority == "Broad - General Construction": score += 2
            scored.append((trade, score))
    return max(scored, key=lambda x: x[1]) if scored else (None, 0)

def infer_status(text: str) -> Optional[str]:
    lower = text.lower()
    if any(w in lower for w in ["canceled", "cancelled", "cancellation", "withdrawn"]): return "Canceled"
    if any(w in lower for w in ["awarded", "recommendation of award", "intent to award", "has been awarded"]): return "Awarded"
    if any(w in lower for w in ["closed", "expired", "no longer accepting", "submission period closed", "deadline has passed", "past bids", "past opportunities"]): return "Closed"
    if any(w in lower for w in OPEN_WORDS): return "Open"
    return None

def contact_info(text: str) -> str:
    found = list(dict.fromkeys(re.findall(EMAIL_PATTERN, text) + re.findall(PHONE_PATTERN, text)))
    return "; ".join(found[:5])

def construction_relevance(text: str) -> int:
    """Scores whether the text is a real bid/construction opportunity in your target universe."""
    lower = text.lower()
    bid_hits = count_matches(text, BID_WORDS)
    doc_hits = count_matches(text, DOCUMENT_WORDS)
    trade_hits = sum(count_matches(text, words) for words in TRADE_KEYWORDS.values())
    general_hits = count_matches(text, CONSTRUCTION_WORDS)

    score = 0
    score += min(25, bid_hits * 3)
    score += min(35, trade_hits * 3)
    score += min(20, general_hits * 2)
    score += min(12, doc_hits * 2)

    if "notice to bidders" in lower or "invitation to bid" in lower: score += 8
    if "prevailing wage" in lower or "certified payroll" in lower: score += 4
    if "bid bond" in lower or "performance bond" in lower or "payment bond" in lower: score += 4

    return max(0, min(100, score))

def date_future(iso: Optional[str]) -> Optional[bool]:
    if not iso: return None
    try: return date_parser.parse(iso).date() >= dt.date.today()
    except Exception: return None

def days_until(iso: Optional[str]) -> Optional[int]:
    if not iso: return None
    try: return (date_parser.parse(iso).date() - dt.date.today()).days
    except Exception: return None

def document_quality(text: str, docs: List[str]) -> Tuple[int, List[str]]:
    """Rewards real paperwork: plans, specs, addenda, bid forms, downloads, PDFs, etc."""
    lower = text.lower()
    reasons = []
    score = 0

    doc_word_hits = count_matches(text, DOCUMENT_WORDS)
    if doc_word_hits:
        pts = min(8, doc_word_hits * 2)
        score += pts; reasons.append(f"+{pts} bid-document language found")

    if docs:
        pts = min(10, len(docs) * 2)
        score += pts; reasons.append(f"+{pts} linked documents/detail pages found")

    pdf_count = sum(1 for d in docs if d.lower().endswith(".pdf"))
    if pdf_count:
        pts = min(6, pdf_count * 2)
        score += pts; reasons.append(f"+{pts} PDF/document links found")

    if any(w in lower for w in ["plans", "drawings", "specifications", "specs", "project manual"]):
        score += 5; reasons.append("+5 plans/specifications/project manual mentioned")

    if any(w in lower for w in ["addendum", "addenda"]):
        score += 2; reasons.append("+2 addenda/addendum language found")

    return min(25, score), reasons

def information_completeness(title: str, county: Optional[str], due_iso: Optional[str], contact: str, docs: List[str], detail_url: str, prebid_iso: Optional[str]) -> Tuple[int, List[str]]:
    """Rewards listings that contain enough information to act on without extra searching."""
    score = 0; reasons = []
    if title and len(title.strip()) >= 10: score += 4; reasons.append("+4 usable title")
    if county: score += 4; reasons.append("+4 county identified")
    if due_iso: score += 8; reasons.append("+8 due date found")
    if contact: score += 8; reasons.append("+8 contact info found")
    if docs: score += 6; reasons.append("+6 paperwork/detail links available")
    if detail_url: score += 3; reasons.append("+3 source/detail URL available")
    if prebid_iso: score += 2; reasons.append("+2 pre-bid/site visit date found")
    return min(25, score), reasons

def score_candidate(c: Candidate, county: Optional[str], trade: Optional[str], trade_strength: int, status: Optional[str], due_iso: Optional[str], prebid_iso: Optional[str], contact: str, docs: List[str], targets: List[str], relevance: int) -> Tuple[int,str,List[str]]:
    """
    100-point score focused on your chosen trades.
    Highest-ranked bids should be open, target-trade matches with due dates, contacts, links, and paperwork.
    """
    score, reasons = 0, []
    lower_targets = [x.lower() for x in targets]
    raw_lower = c.raw_text.lower()

    # 1) Status / deadline: open opportunities matter most.
    future, days = date_future(due_iso), days_until(due_iso)
    if status == "Open": score += 18; reasons.append("+18 status appears open")
    elif status in {"Closed", "Canceled", "Awarded"}: score -= 45; reasons.append(f"-45 status is {status}")

    if future is True:
        if days is not None and 3 <= days <= 45:
            score += 22; reasons.append(f"+22 due date is open and actionable: {days} days away")
        elif days is not None and 0 <= days < 3:
            score += 8; reasons.append(f"+8 due date is open but urgent: {days} days away")
        else:
            score += 14; reasons.append("+14 future due date")
    elif future is False:
        score -= 50; reasons.append("-50 expired due date")
    else:
        score -= 25; reasons.append("-25 no due date found")

    # 2) Trade targeting: most important positive category.
    priority = trade_priority(trade)
    if trade:
        strength_pts = min(16, max(4, trade_strength))
        score += strength_pts; reasons.append(f"+{strength_pts} trade keyword strength: {trade}")
    else:
        score -= 18; reasons.append("-18 no target trade detected")

    boost = TRADE_PRIORITY_BOOSTS.get(priority, 0)
    if boost:
        score += boost; reasons.append(f"+{boost} {priority}: {trade}")
    else:
        score -= 20; reasons.append("-20 not one of the prioritized trades")

    # 3) County / geography.
    if county and county.lower() in lower_targets:
        score += 12; reasons.append(f"+12 target county: {county}")
    elif c.source_county.lower() == "statewide":
        score += 4; reasons.append("+4 statewide source")
    else:
        score -= 8; reasons.append("-8 county not clearly targeted")

    # 4) Bid/construction relevance.
    if relevance >= 75: score += 14; reasons.append("+14 very strong bid/construction relevance")
    elif relevance >= 55: score += 10; reasons.append("+10 strong bid/construction relevance")
    elif relevance >= 35: score += 5; reasons.append("+5 moderate bid/construction relevance")
    else: score -= 15; reasons.append("-15 weak bid/construction relevance")

    # 5) Information completeness and paperwork.
    info_pts, info_reasons = information_completeness(c.title, county, due_iso, contact, docs, c.detail_url, prebid_iso)
    score += info_pts; reasons.extend(info_reasons)

    doc_pts, doc_reasons = document_quality(c.raw_text, docs)
    score += doc_pts; reasons.extend(doc_reasons)

    # 6) Pre-bid / site visit timing.
    pf = date_future(prebid_iso)
    if prebid_iso and pf is True:
        score += 4; reasons.append("+4 future pre-bid/site visit")
    elif prebid_iso and pf is False:
        score -= 8; reasons.append("-8 pre-bid/site visit already passed")

    # 7) Negative/stale/result language.
    if contains_any(c.raw_text, NEGATIVE_WORDS):
        score -= 25; reasons.append("-25 closed/archive/award/cancel/result language found")

    # 8) Guardrail: do not let non-target trades look hot just because they have documents.
    if priority == "Unclassified" and score > 54:
        reasons.append("Score capped at 54 because no prioritized trade was detected")
        score = 54
    if future is False and score > 39:
        reasons.append("Score capped at 39 because due date is expired")
        score = 39
    if status in {"Closed", "Canceled", "Awarded"} and score > 39:
        reasons.append(f"Score capped at 39 because status is {status}")
        score = 39

    score = max(0, min(100, score))
    band = "HOT" if score >= 82 else "REVIEW" if score >= 62 else "LOW" if score >= 35 else "ARCHIVE"
    return score, band, reasons

def dedupe_key(title: str, source: str, due: Optional[str], county: Optional[str]) -> str:
    base = f"{title} {source} {due or ''} {county or ''}".lower()
    base = re.sub(r"[^a-z0-9 ]+", " ", base); base = re.sub(r"\b(bid|bids|rfp|rfq|notice|project|the|and|of|for|to|at)\b", " ", base)
    return hashlib.sha1(normalize_ws(base).encode("utf-8")).hexdigest()

def normalize(c: Candidate, run_id: str, targets: List[str]) -> Listing:
    raw = normalize_ws(c.raw_text); title = truncate(c.title or derive_title(raw) or c.source_name, 180)
    due_raw, due_iso = parse_date(raw, DATE_PATTERNS); pre_raw, pre_iso = parse_date(raw, PREBID_PATTERNS)
    county = infer_county(raw, c.source_county, targets); trade, strength = infer_trade(raw); priority = trade_priority(trade); status = infer_status(raw)
    contact = contact_info(raw); docs = list(dict.fromkeys(u for u in c.linked_documents if u))[:30]
    rel = construction_relevance(raw); score, band, reasons = score_candidate(c, county, trade, strength, status, due_iso, pre_iso, contact, docs, targets, rel)
    return Listing(run_id, c.source_id, c.source_name, c.source_county, c.source_url, c.source_type, c.scraper_type, title, c.detail_url, county, trade, priority, status, due_raw, due_iso, pre_raw, pre_iso, truncate(raw, 600), contact, docs, rel, score, band, reasons, raw, sha(c.detail_url+raw), dedupe_key(title,c.source_name,due_iso,county), now_utc())

def ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    existing = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        conn.commit()

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True); conn = sqlite3.connect(path); conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, source_count INTEGER, sources_successful INTEGER, sources_failed INTEGER, candidates_found INTEGER, listings_saved INTEGER, duplicates_skipped INTEGER, report_csv TEXT, report_html TEXT, report_json TEXT, log_path TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS source_results(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source_id TEXT, source_name TEXT, url TEXT, success INTEGER, candidates_found INTEGER, error_message TEXT, checked_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS listings(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, source_id TEXT, source_name TEXT, source_county TEXT, source_url TEXT, source_type TEXT, scraper_type TEXT, title TEXT, detail_url TEXT, county TEXT, trade_category TEXT, trade_priority TEXT, status TEXT, due_date_raw TEXT, due_date_iso TEXT, prebid_date_raw TEXT, prebid_date_iso TEXT, summary TEXT, contact_info TEXT, linked_documents_json TEXT, construction_relevance INTEGER, score INTEGER, score_band TEXT, score_reasons_json TEXT, raw_text TEXT, raw_hash TEXT UNIQUE, dedupe_key TEXT, scraped_at TEXT)""")
    ensure_column(conn, "listings", "trade_priority", "TEXT"); conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_run ON listings(run_id)"); conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(score)"); conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_due ON listings(due_date_iso)"); conn.commit(); return conn

def start_run(conn, run_id, source_count, log_path):
    conn.execute("INSERT INTO runs(run_id,started_at,source_count,sources_successful,sources_failed,candidates_found,listings_saved,duplicates_skipped,log_path) VALUES(?,?,?,0,0,0,0,0,?)", (run_id, now_utc(), source_count, str(log_path))); conn.commit()
def source_result(conn, run_id, s: Source, success: bool, count: int, err: Optional[str]=None):
    conn.execute("INSERT INTO source_results(run_id,source_id,source_name,url,success,candidates_found,error_message,checked_at) VALUES(?,?,?,?,?,?,?,?)", (run_id,s.id,s.name,s.url,int(success),count,err,now_utc())); conn.commit()
def store_listing(conn, l: Listing) -> bool:
    try:
        conn.execute("""INSERT INTO listings(run_id,source_id,source_name,source_county,source_url,source_type,scraper_type,title,detail_url,county,trade_category,trade_priority,status,due_date_raw,due_date_iso,prebid_date_raw,prebid_date_iso,summary,contact_info,linked_documents_json,construction_relevance,score,score_band,score_reasons_json,raw_text,raw_hash,dedupe_key,scraped_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (l.run_id,l.source_id,l.source_name,l.source_county,l.source_url,l.source_type,l.scraper_type,l.title,l.detail_url,l.county,l.trade_category,l.trade_priority,l.status,l.due_date_raw,l.due_date_iso,l.prebid_date_raw,l.prebid_date_iso,l.summary,l.contact_info,json.dumps(l.linked_documents),l.construction_relevance,l.score,l.score_band,json.dumps(l.score_reasons),l.raw_text,l.raw_hash,l.dedupe_key,l.scraped_at)); conn.commit(); return True
    except sqlite3.IntegrityError: return False

def df_for_run(conn, run_id):
    return pd.read_sql_query("""SELECT score_band,score,title,source_name,county,trade_category,trade_priority,status,due_date_iso,prebid_date_iso,contact_info,detail_url,summary,construction_relevance,score_reasons_json,linked_documents_json,dedupe_key,scraped_at FROM listings WHERE run_id=? ORDER BY score DESC, due_date_iso ASC""", conn, params=(run_id,))
def sources_for_run(conn, run_id):
    return pd.read_sql_query("SELECT source_name,url,success,candidates_found,error_message,checked_at FROM source_results WHERE run_id=? ORDER BY source_name", conn, params=(run_id,))
def latest_run_id(conn):
    row = conn.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone(); return row[0] if row else None

def summary(df, sdf, run_id):
    if df.empty: return {"run_id": run_id, "total_listings": 0, "message": "No listings saved."}
    return {"run_id": run_id, "total_listings": int(len(df)), "successful_sources": int(sdf["success"].sum()) if not sdf.empty else 0, "failed_sources": int((sdf["success"]==0).sum()) if not sdf.empty else 0, "score_stats": {"min": int(df.score.min()), "max": int(df.score.max()), "average": round(float(df.score.mean()),2), "median": round(float(df.score.median()),2)}, "score_band_counts": df.score_band.value_counts().to_dict(), "county_counts": df.county.fillna("Unknown").value_counts().to_dict(), "trade_counts": df.trade_category.fillna("Unknown").value_counts().to_dict(), "priority_counts": df.trade_priority.fillna("Unclassified").value_counts().to_dict(), "source_counts": df.source_name.value_counts().to_dict(), "top_10_by_score": df.head(10)[["score","score_band","title","source_name","county","trade_category","trade_priority","due_date_iso","detail_url"]].to_dict(orient="records")}

def esc(x): return "" if x is None else html.escape(str(x))
def report_html(df, sdf, summ, run_id):
    bands = summ.get("score_band_counts", {}); stats = summ.get("score_stats", {})
    rows = []
    if df.empty: rows.append('<tr><td colspan="10">No listings saved.</td></tr>')
    for rank, (_, r) in enumerate(df.iterrows(), start=1):
        try: reasons = json.loads(r.score_reasons_json or "[]")
        except Exception: reasons = []
        try: docs = json.loads(r.linked_documents_json or "[]")
        except Exception: docs = []
        reason_html = "<br>".join(esc(x) for x in reasons[:8])
        docs_html = "<br>".join(f'<a href="{esc(u)}" target="_blank">document</a>' for u in docs[:3])
        rows.append(f"""<tr><td>{rank}</td><td><span class="band {esc(r.score_band)}">{esc(r.score_band)}</span><br><b>{esc(r.score)}</b></td><td><a href="{esc(r.detail_url)}" target="_blank">{esc(r.title)}</a><br><span class="muted">{esc(r.source_name)}</span></td><td>{esc(r.county)}</td><td>{esc(r.trade_category)}</td><td>{esc(r.trade_priority)}</td><td>{esc(r.status)}</td><td>{esc(r.due_date_iso)}<br><span class="muted">Pre-bid: {esc(r.prebid_date_iso)}</span></td><td>{esc(r.summary)}<br>{docs_html}</td><td class="reasons">{reason_html}</td></tr>""")
    srows = []
    for _, r in sdf.iterrows():
        stat = "Success" if int(r.success)==1 else "Failed"
        srows.append(f"<tr><td>{esc(stat)}</td><td><a href='{esc(r.url)}' target='_blank'>{esc(r.source_name)}</a></td><td>{esc(r.candidates_found)}</td><td>{esc(r.error_message)}</td><td>{esc(r.checked_at)}</td></tr>")
    county_items = ''.join(f"<li>{esc(k)}: {esc(v)}</li>" for k,v in summ.get('county_counts',{}).items())
    trade_items = ''.join(f"<li>{esc(k)}: {esc(v)}</li>" for k,v in summ.get('trade_counts',{}).items())
    priority_items = ''.join(f"<li>{esc(k)}: {esc(v)}</li>" for k,v in summ.get('priority_counts',{}).items())
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>NJ Bid Report {esc(run_id)}</title><style>body{{font-family:Arial,sans-serif;margin:24px;background:#fafafa;color:#222}}.muted{{color:#666;font-size:.9em}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:18px 0}}.card{{background:#fff;border:1px solid #ddd;border-radius:10px;padding:14px}}.big{{font-size:1.8em;font-weight:bold}}table{{width:100%;border-collapse:collapse;margin-top:16px;background:#fff}}th,td{{border:1px solid #ddd;padding:8px;vertical-align:top;font-size:.92em}}th{{background:#f0f0f0;text-align:left}}.band{{display:inline-block;padding:3px 7px;border-radius:6px;font-size:.8em;font-weight:bold;background:#eee}}.HOT{{background:#d8f5dd}}.REVIEW{{background:#fff2bf}}.LOW{{background:#e2ecff}}.ARCHIVE{{background:#eee}}.reasons{{font-size:.84em;color:#444}}.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:18px}}</style></head><body><h1>NJ Public Bid Scraper Report</h1><p class="muted">Run ID: {esc(run_id)} | Generated: {esc(dt.datetime.now().strftime('%Y-%m-%d %I:%M %p'))} | No AI used</p><div class="cards"><div class="card"><div class="muted">Total Listings</div><div class="big">{esc(summ.get('total_listings',0))}</div></div><div class="card"><div class="muted">HOT</div><div class="big">{esc(bands.get('HOT',0))}</div></div><div class="card"><div class="muted">REVIEW</div><div class="big">{esc(bands.get('REVIEW',0))}</div></div><div class="card"><div class="muted">LOW</div><div class="big">{esc(bands.get('LOW',0))}</div></div><div class="card"><div class="muted">ARCHIVE</div><div class="big">{esc(bands.get('ARCHIVE',0))}</div></div></div><div class="two"><div class="card"><h2>Score Comparison</h2><p>Max: <b>{esc(stats.get('max',''))}</b></p><p>Average: <b>{esc(stats.get('average',''))}</b></p><p>Median: <b>{esc(stats.get('median',''))}</b></p><p>Min: <b>{esc(stats.get('min',''))}</b></p></div><div class="card"><h2>By County</h2><ul>{county_items}</ul></div><div class="card"><h2>By Trade</h2><ul>{trade_items}</ul></div><div class="card"><h2>By Priority</h2><ul>{priority_items}</ul></div></div><h2>Ranked Bid Listings</h2><table><thead><tr><th>Rank</th><th>Score</th><th>Project / Source</th><th>County</th><th>Trade</th><th>Priority</th><th>Status</th><th>Dates</th><th>Summary / Docs</th><th>Score Reasons</th></tr></thead><tbody>{''.join(rows)}</tbody></table><h2>Source Run Details</h2><table><thead><tr><th>Status</th><th>Source</th><th>Candidates</th><th>Error</th><th>Checked</th></tr></thead><tbody>{''.join(srows)}</tbody></table></body></html>"""

def write_reports(conn, run_id, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True); df = df_for_run(conn, run_id); sdf = sources_for_run(conn, run_id); summ = summary(df, sdf, run_id)
    csv_path = out_dir / f"bid_report_{run_id}.csv"; html_path = out_dir / f"bid_report_{run_id}.html"; json_path = out_dir / f"run_summary_{run_id}.json"
    export = df.copy()
    if not export.empty:
        export["score_reasons"] = export.score_reasons_json.apply(lambda x: "; ".join(json.loads(x or "[]")))
        export["linked_documents"] = export.linked_documents_json.apply(lambda x: "; ".join(json.loads(x or "[]")))
        export = export.drop(columns=["score_reasons_json","linked_documents_json"])
    export.to_csv(csv_path, index=False); html_path.write_text(report_html(df, sdf, summ, run_id), encoding="utf-8"); json_path.write_text(json.dumps(summ, indent=2), encoding="utf-8")
    return csv_path, html_path, json_path

def finish_run(conn, run_id, ok, failed, candidates, saved, dupes, csv, htmlp, js):
    conn.execute("UPDATE runs SET finished_at=?,sources_successful=?,sources_failed=?,candidates_found=?,listings_saved=?,duplicates_skipped=?,report_csv=?,report_html=?,report_json=? WHERE run_id=?", (now_utc(),ok,failed,candidates,saved,dupes,str(csv),str(htmlp),str(js),run_id)); conn.commit()

def run(config: Path, db: Path, out: Path, logs: Path):
    run_id = run_stamp(); log_path = setup_logging(logs, run_id)
    settings, sources = load_config(config); targets = settings.get("target_counties", []); delay = float(settings.get("request_delay_seconds", 1.0)); timeout = int(settings.get("timeout_seconds", 30)); max_detail = int(settings.get("max_detail_pages_per_source", 8))
    conn = init_db(db); start_run(conn, run_id, len(sources), log_path); s = session()
    logging.info("Started run %s with %s active sources", run_id, len(sources))
    ok=failed=candidates_total=saved=dupes=0
    for i, source in enumerate(sources, 1):
        logging.info("[%s/%s] Scraping %s", i, len(sources), source.name)
        try:
            candidates = scrape_source(s, source, timeout, max_detail); candidates_total += len(candidates); ok += 1; source_result(conn, run_id, source, True, len(candidates))
            logging.info("Found %s candidates", len(candidates))
            for cand in candidates:
                listing = normalize(cand, run_id, targets)
                if store_listing(conn, listing): saved += 1; logging.info("Saved score=%s title=%s", listing.score, listing.title)
                else: dupes += 1; logging.info("Duplicate skipped title=%s", listing.title)
        except Exception as e:
            failed += 1; source_result(conn, run_id, source, False, 0, f"{type(e).__name__}: {e}"); logging.exception("Source failed: %s", source.name)
        time.sleep(delay)
    csv, htmlp, js = write_reports(conn, run_id, out); finish_run(conn, run_id, ok, failed, candidates_total, saved, dupes, csv, htmlp, js)
    print("\nDONE"); print(f"Run ID: {run_id}"); print(f"Sources successful: {ok}"); print(f"Sources failed: {failed}"); print(f"Candidates found: {candidates_total}"); print(f"Listings saved: {saved}"); print(f"Duplicates skipped: {dupes}"); print(f"CSV report: {csv}"); print(f"HTML report: {htmlp}"); print(f"JSON summary: {js}"); print(f"Log file: {log_path}")

def regenerate(db: Path, out: Path, run_id: Optional[str], latest: bool):
    conn = init_db(db)
    if latest: run_id = latest_run_id(conn)
    if not run_id: raise SystemExit("No run_id supplied and no latest run found.")
    csv, htmlp, js = write_reports(conn, run_id, out); print(f"Report regenerated for {run_id}\nCSV: {csv}\nHTML: {htmlp}\nJSON: {js}")

def main(argv=None):
    p = argparse.ArgumentParser(description="Repeatable no-AI scraper for NJ public bid listings."); sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--config", type=Path, default=Path("config/sources.yaml")); r.add_argument("--db", type=Path, default=Path("data/bids.sqlite")); r.add_argument("--out", type=Path, default=Path("output")); r.add_argument("--logs", type=Path, default=Path("logs"))
    rep = sub.add_parser("report"); rep.add_argument("--db", type=Path, default=Path("data/bids.sqlite")); rep.add_argument("--out", type=Path, default=Path("output")); rep.add_argument("--run-id"); rep.add_argument("--latest-run", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "run": run(a.config, a.db, a.out, a.logs)
    elif a.cmd == "report": regenerate(a.db, a.out, a.run_id, a.latest_run)
    return 0
if __name__ == "__main__": raise SystemExit(main())
