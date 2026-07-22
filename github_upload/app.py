from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, List

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


BASE_DIR = Path(__file__).parent
SCRAPER_FILE = BASE_DIR / "nj_bid_scraper.py"
CONFIG_FILE = BASE_DIR / "config" / "sources.yaml"
DB_FILE = BASE_DIR / "data" / "bids.sqlite"
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"
PDF_FILE = OUTPUT_DIR / "bid_report_latest.pdf"


PRIORITY_TRADE_KEYWORDS = {
    "Masonry": [
        "masonry", "masonry restoration", "brick", "brickwork", "brick work", "brick repair",
        "block", "cmu", "stone", "stonework", "stone repair", "terra cotta", "terracotta",
        "parapet", "lintel", "lintel replacement", "lintel repair", "mortar", "mortar repair",
        "mortar joints", "mortar joint", "facade masonry", "exterior wall repair",
    ],
    "Waterproofing": [
        "waterproof", "waterproofing", "water proofing", "below grade waterproofing",
        "below-grade waterproofing", "foundation waterproofing", "dampproofing", "damp proofing",
        "water intrusion", "leak repair", "joint sealant", "sealant", "caulking", "expansion joint",
        "traffic coating", "deck coating", "plaza deck", "waterproof membrane", "liquid membrane",
        "fluid applied waterproofing", "fluid-applied waterproofing", "cold applied membrane",
        "cold-applied membrane", "liquid applied membrane", "fluid applied membrane",
    ],
    "Tuck Pointing": [
        "tuck pointing", "tuckpointing", "tuck-pointing", "tuck point", "tuckpoint",
        "repointing", "re-pointing", "pointing", "mortar joint repair", "mortar joints",
        "mortar restoration", "brick pointing", "stone pointing", "joint repointing",
    ],
    "Roof Restoration": [
        "roof restoration", "roof coating", "roof coatings", "silicone roof", "silicone roof coating",
        "silicone roof restoration", "silicone coating", "fluid applied roof", "fluid-applied roof",
        "roof rehabilitation", "roof restoration system", "roof recover", "recover roof",
        "roof repair", "roof repairs", "roof maintenance", "roof membrane restoration",
        "elastomeric roof", "liquid applied roof", "liquid-applied roof",
    ],
    "Structural Reinforcement": [
        "structural reinforcement", "structural strengthening", "structural upgrade",
        "carbon fiber reinforcement", "carbon fibre reinforcement", "frp", "frp reinforcement",
        "fiber reinforced polymer", "fibre reinforced polymer", "steel reinforcement",
        "beam reinforcement", "column reinforcement", "slab reinforcement", "wall reinforcement",
        "structural steel reinforcement", "reinforcing steel", "strengthening work",
    ],
    "Structural Alterations": [
        "structural alteration", "structural alterations", "structural modification",
        "structural modifications", "structural repair", "structural repairs", "bearing wall",
        "load bearing", "load-bearing", "beam replacement", "column replacement", "shoring",
        "underpinning", "foundation underpinning", "new opening", "wall opening",
        "structural opening", "temporary shoring", "structural demolition", "structural framing",
    ],
    "Interior Renovations": [
        "interior renovation", "interior renovations", "interior rehab", "interior rehabilitation",
        "interior construction", "interior improvements", "fit out", "fit-out", "tenant fit out",
        "tenant fit-out", "office renovation", "classroom renovation", "lobby renovation",
        "restroom renovation", "bathroom renovation", "interior fitout", "interior fit-out",
    ],
    "Interior Alterations": [
        "interior alteration", "interior alterations", "interior modification",
        "interior modifications", "interior reconfiguration", "partition", "partitions",
        "drywall", "gypsum board", "gwb", "interior finishes", "ceiling work",
        "acoustical ceiling", "acoustic ceiling", "dropped ceiling", "suspended ceiling",
    ],
    "Building/General Construction": [
        "building/general construction", "general construction", "building construction",
        "building improvements", "building addition", "addition", "additions", "new addition",
        "sitework", "site work", "concrete", "concrete repair", "concrete restoration",
        "concrete slab", "foundation", "roofing", "roof replacement", "building envelope",
        "exterior improvements", "facility improvements", "alterations and additions",
    ],
}

PRIORITY_TRADE_ORDER = list(PRIORITY_TRADE_KEYWORDS.keys())
CLOSED_STATUSES = {"closed", "canceled", "cancelled", "awarded", "expired"}


st.set_page_config(
    page_title="NJ Bid Scraper Dashboard",
    page_icon="🏗️",
    layout="wide",
)


st.markdown(
    """
    <style>
        .block-container {
            padding-top: 1.4rem;
            padding-bottom: 2rem;
        }
        .hero-card {
            background: linear-gradient(135deg, #172033 0%, #253652 52%, #355070 100%);
            color: white;
            padding: 1.4rem 1.6rem;
            border-radius: 18px;
            margin-bottom: 1rem;
            box-shadow: 0 8px 22px rgba(23, 32, 51, 0.18);
        }
        .hero-card h1 {
            margin: 0;
            font-size: 2.05rem;
        }
        .hero-card p {
            margin: .55rem 0 0 0;
            opacity: .88;
            font-size: 1rem;
        }
        .info-card {
            background: #ffffff;
            border: 1px solid #e8edf5;
            border-radius: 16px;
            padding: 1rem 1.05rem;
            box-shadow: 0 2px 10px rgba(16, 24, 40, 0.05);
            height: 100%;
        }
        .small-muted {
            color: #667085;
            font-size: .88rem;
        }
        .tag {
            display: inline-block;
            padding: 0.22rem 0.5rem;
            margin: 0.12rem 0.15rem 0.12rem 0;
            border-radius: 999px;
            background: #eef4ff;
            color: #1849a9;
            border: 1px solid #c7d7fe;
            font-size: .78rem;
            font-weight: 600;
        }
        .warning-tag {
            display: inline-block;
            padding: 0.22rem 0.5rem;
            margin: 0.12rem 0.15rem 0.12rem 0;
            border-radius: 999px;
            background: #fff4e5;
            color: #b54708;
            border: 1px solid #fedf89;
            font-size: .78rem;
            font-weight: 600;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.65rem;
        }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_admin_password() -> str:
    """
    Gets the admin password from Streamlit secrets if deployed,
    otherwise uses a local fallback password.
    """
    try:
        return st.secrets.get("ADMIN_PASSWORD", "change-this-password")
    except Exception:
        return "change-this-password"


def is_admin() -> bool:
    st.sidebar.header("Admin Access")
    password = st.sidebar.text_input("Admin password", type="password")
    return password == get_admin_password()


def latest_file(pattern: str) -> Path | None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    files = list(OUTPUT_DIR.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda file: file.stat().st_mtime)


def latest_log_file() -> Path | None:
    LOGS_DIR.mkdir(exist_ok=True)
    files = list(LOGS_DIR.glob("*.log"))
    if not files:
        return None
    return max(files, key=lambda file: file.stat().st_mtime)


def reset_outputs() -> None:
    """
    Deletes the database, reports, and logs so the next scraper run starts fresh.
    """
    DB_FILE.unlink(missing_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)

    for folder in [OUTPUT_DIR, LOGS_DIR]:
        for item in folder.iterdir():
            if item.is_file():
                item.unlink()


def run_scraper() -> subprocess.CompletedProcess[str]:
    """
    Runs the existing scraper file.
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    DB_FILE.parent.mkdir(exist_ok=True)

    command = [
        sys.executable,
        str(SCRAPER_FILE),
        "run",
        "--config",
        str(CONFIG_FILE),
        "--db",
        str(DB_FILE),
        "--out",
        str(OUTPUT_DIR),
    ]

    return subprocess.run(
        command,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )


def load_latest_csv() -> tuple[Path | None, pd.DataFrame | None]:
    csv_file = latest_file("*.csv")
    if csv_file is None:
        return None, None
    df = pd.read_csv(csv_file)
    return csv_file, df


def load_latest_summary() -> dict:
    json_file = latest_file("run_summary_*.json")
    if json_file is None:
        return {}
    try:
        return json.loads(json_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def safe_str(value) -> str:
    if pd.isna(value):
        return ""
    return str(value)


def combined_text_for_row(row: pd.Series) -> str:
    parts = []
    for col in [
        "trade_category",
        "trade_priority",
        "title",
        "summary",
        "source_name",
        "score_reasons",
        "detail_url",
    ]:
        if col in row.index:
            parts.append(safe_str(row.get(col)))
    return " ".join(parts).lower().replace("-", " ")


def keyword_matches(text: str, keywords: Iterable[str]) -> bool:
    normalized = re.sub(r"\s+", " ", text.lower().replace("-", " "))
    for keyword in keywords:
        kw = re.sub(r"\s+", " ", keyword.lower().replace("-", " ")).strip()
        if kw and kw in normalized:
            return True
    return False


def detect_priority_trade_matches(row: pd.Series) -> List[str]:
    text = combined_text_for_row(row)
    matches = []
    for trade, keywords in PRIORITY_TRADE_KEYWORDS.items():
        if keyword_matches(text, keywords):
            matches.append(trade)
    return matches


def parse_due_date(series: pd.Series) -> pd.Series:
    if series.empty:
        return pd.Series(dtype="datetime64[ns]")
    return pd.to_datetime(series, errors="coerce")


def make_missing_info(row: pd.Series) -> str:
    missing = []
    if not safe_str(row.get("due_date_iso")):
        missing.append("due date")
    if not safe_str(row.get("contact_info")):
        missing.append("contact")
    if not safe_str(row.get("detail_url")):
        missing.append("website link")

    docs_text = " ".join([safe_str(row.get("linked_documents")), safe_str(row.get("linked_documents_json"))])
    if len(docs_text.strip()) < 4:
        missing.append("documents")

    if not missing:
        return "Complete"
    return "Missing: " + ", ".join(missing)


def prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for col in [
        "score_band",
        "score",
        "title",
        "source_name",
        "county",
        "trade_category",
        "trade_priority",
        "status",
        "due_date_iso",
        "prebid_date_iso",
        "contact_info",
        "detail_url",
        "summary",
        "linked_documents",
        "score_reasons",
    ]:
        if col not in df.columns:
            df[col] = ""

    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0).astype(int)
    df["due_date_parsed"] = parse_due_date(df["due_date_iso"])

    today = pd.Timestamp.today().normalize()
    df["days_until_due"] = (df["due_date_parsed"].dt.normalize() - today).dt.days

    df["priority_trade_match_list"] = df.apply(detect_priority_trade_matches, axis=1)
    df["priority_trade_matches"] = df["priority_trade_match_list"].apply(
        lambda items: "; ".join(items) if items else "No priority match"
    )

    df["has_priority_trade"] = df["priority_trade_match_list"].apply(bool)
    df["has_contact"] = df["contact_info"].fillna("").astype(str).str.len() > 0
    df["has_website_link"] = df["detail_url"].fillna("").astype(str).str.startswith("http")

    document_text = (
        df.get("linked_documents", "").fillna("").astype(str)
        + " "
        + df.get("linked_documents_json", "").fillna("").astype(str)
    )
    df["has_documents"] = document_text.str.len() > 4

    status_lower = df["status"].fillna("").astype(str).str.lower()
    df["appears_open"] = ~status_lower.isin(CLOSED_STATUSES) & (
        df["due_date_parsed"].isna() | (df["due_date_parsed"].dt.normalize() >= today)
    )

    df["missing_info"] = df.apply(make_missing_info, axis=1)
    df["action_level"] = df.apply(action_level, axis=1)

    return df.sort_values(["score", "has_priority_trade", "appears_open"], ascending=[False, False, False])


def action_level(row: pd.Series) -> str:
    score = int(row.get("score", 0) or 0)
    has_priority = bool(row.get("has_priority_trade", False))
    appears_open = bool(row.get("appears_open", False))
    missing_info = safe_str(row.get("missing_info"))

    if appears_open and has_priority and score >= 80 and missing_info == "Complete":
        return "Bid Review Now"
    if appears_open and has_priority and score >= 65:
        return "High Priority Follow-Up"
    if appears_open and score >= 55:
        return "Review"
    if not appears_open:
        return "Likely Closed / Verify"
    return "Watch"


def create_simple_pdf_from_csv(filtered_df: pd.DataFrame | None = None) -> Path:
    """
    Creates a PDF report directly from the latest CSV/report dataframe.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise RuntimeError(
            "ReportLab is required to create PDF reports. Install it with 'pip install reportlab'."
        ) from exc

    csv_file, df = load_latest_csv()
    if csv_file is None or df is None:
        raise FileNotFoundError("No CSV report found. Run the scraper first.")

    if filtered_df is not None and not filtered_df.empty:
        pdf_df = filtered_df.copy()
    else:
        pdf_df = prepare_dataframe(df)

    OUTPUT_DIR.mkdir(exist_ok=True)

    doc = SimpleDocTemplate(
        str(PDF_FILE),
        pagesize=landscape(letter),
        rightMargin=20,
        leftMargin=20,
        topMargin=20,
        bottomMargin=20,
    )

    styles = getSampleStyleSheet()
    story = [
        Paragraph("NJ Public Bid Scraper Report", styles["Title"]),
        Spacer(1, 8),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}", styles["Normal"]),
        Paragraph(f"Source CSV: {csv_file.name}", styles["Normal"]),
        Spacer(1, 10),
    ]

    display_columns = [
        "score_band",
        "score",
        "action_level",
        "priority_trade_matches",
        "title",
        "source_name",
        "county",
        "status",
        "due_date_iso",
        "missing_info",
        "detail_url",
    ]
    display_columns = [col for col in display_columns if col in pdf_df.columns]

    pdf_df = pdf_df[display_columns].head(75).copy()
    for col in pdf_df.columns:
        pdf_df[col] = pdf_df[col].astype(str).str.slice(0, 95)

    table_data = [list(pdf_df.columns)]
    for row in pdf_df.values.tolist():
        table_data.append([Paragraph(str(cell), styles["BodyText"]) for cell in row])

    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF7")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ]
        )
    )

    story.append(table)
    doc.build(story)
    return PDF_FILE


def show_download_button(label: str, file_path: Path | None, mime_type: str) -> None:
    if file_path and Path(file_path).exists():
        with open(file_path, "rb") as file:
            st.download_button(
                label=label,
                data=file,
                file_name=Path(file_path).name,
                mime=mime_type,
                use_container_width=True,
            )


def display_pdf_preview(pdf_path: Path) -> None:
    if not pdf_path.exists():
        st.info("Create a PDF first.")
        return
    encoded = base64.b64encode(pdf_path.read_bytes()).decode("utf-8")
    components.html(
        f'<iframe src="data:application/pdf;base64,{encoded}" width="100%" height="760" type="application/pdf"></iframe>',
        height=780,
    )


def render_trade_tags(trades: Iterable[str]) -> str:
    return "".join(f'<span class="tag">{trade}</span>' for trade in trades)


def render_missing_tags(missing_text: str) -> str:
    if missing_text == "Complete":
        return '<span class="tag">Complete info</span>'
    return "".join(f'<span class="warning-tag">{part.strip()}</span>' for part in missing_text.replace("Missing:", "").split(",") if part.strip())


def filter_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    st.subheader("Filter Results")

    filtered_df = df.copy()

    search = st.text_input(
        "Search project text",
        placeholder="Example: masonry, roof, waterproofing, school, Newark, concrete...",
    )
    if search:
        pattern = re.escape(search.strip())
        searchable = filtered_df[["title", "summary", "source_name", "county", "priority_trade_matches"]].fillna("").agg(" ".join, axis=1)
        filtered_df = filtered_df[searchable.str.contains(pattern, case=False, regex=True)]

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        if "score_band" in filtered_df.columns:
            bands = sorted(df["score_band"].dropna().astype(str).unique())
            default_bands = [band for band in ["HOT", "REVIEW"] if band in bands] or bands
            selected_bands = st.multiselect("Score Band", options=bands, default=default_bands)
            if selected_bands:
                filtered_df = filtered_df[filtered_df["score_band"].isin(selected_bands)]

    with col2:
        counties = sorted(df["county"].dropna().astype(str).unique())
        selected_counties = st.multiselect("County", options=counties, default=counties)
        if selected_counties:
            filtered_df = filtered_df[filtered_df["county"].isin(selected_counties)]

    with col3:
        selected_trades = st.multiselect(
            "Priority Trade Match",
            options=PRIORITY_TRADE_ORDER,
            default=[trade for trade in PRIORITY_TRADE_ORDER if df["priority_trade_match_list"].apply(lambda items: trade in items).any()],
            help="This replaces the old Primary/Secondary/Standard filter with the individual trades you care about.",
        )
        if selected_trades:
            filtered_df = filtered_df[
                filtered_df["priority_trade_match_list"].apply(lambda matches: any(t in matches for t in selected_trades))
            ]

    with col4:
        action_options = sorted(df["action_level"].dropna().astype(str).unique())
        selected_actions = st.multiselect(
            "Action Level",
            options=action_options,
            default=[a for a in action_options if a in {"Bid Review Now", "High Priority Follow-Up", "Review"}] or action_options,
        )
        if selected_actions:
            filtered_df = filtered_df[filtered_df["action_level"].isin(selected_actions)]

    col5, col6, col7, col8 = st.columns(4)

    with col5:
        min_score = st.slider("Minimum Score", 0, 100, 55, 5)
        filtered_df = filtered_df[filtered_df["score"] >= min_score]

    with col6:
        due_window = st.selectbox(
            "Due Date Window",
            ["Any", "Next 7 days", "Next 14 days", "Next 30 days", "Next 45 days", "Next 60 days"],
            index=0,
        )
        if due_window != "Any":
            max_days = int(re.search(r"\d+", due_window).group())
            filtered_df = filtered_df[
                filtered_df["days_until_due"].notna()
                & (filtered_df["days_until_due"] >= 0)
                & (filtered_df["days_until_due"] <= max_days)
            ]

    with col7:
        open_only = st.checkbox("Open / not expired only", value=True)
        if open_only:
            filtered_df = filtered_df[filtered_df["appears_open"]]

    with col8:
        complete_only = st.checkbox("Complete info only", value=False)
        if complete_only:
            filtered_df = filtered_df[filtered_df["missing_info"] == "Complete"]

    with st.expander("More contractor filters"):
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.checkbox("Must have contact info", value=False):
                filtered_df = filtered_df[filtered_df["has_contact"]]
        with c2:
            if st.checkbox("Must have website link", value=False):
                filtered_df = filtered_df[filtered_df["has_website_link"]]
        with c3:
            if st.checkbox("Must have document links", value=False):
                filtered_df = filtered_df[filtered_df["has_documents"]]

        sources = sorted(df["source_name"].dropna().astype(str).unique())
        selected_sources = st.multiselect("Limit to source/agency", options=sources, default=[])
        if selected_sources:
            filtered_df = filtered_df[filtered_df["source_name"].isin(selected_sources)]

    return filtered_df


st.markdown(
    """
    <div class="hero-card">
        <h1>🏗️ NJ Public Bid Scraper Dashboard</h1>
        <p>Find, rank, filter, and package public bid opportunities that match your preferred construction trades.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

admin = is_admin()

st.sidebar.divider()
st.sidebar.subheader("Priority Trades")
st.sidebar.markdown(render_trade_tags(PRIORITY_TRADE_ORDER), unsafe_allow_html=True)
st.sidebar.divider()

if not admin:
    st.sidebar.warning("Enter the admin password to run or reset the scraper.")
else:
    st.sidebar.success("Admin access granted.")

latest_html = latest_file("*.html")
latest_csv = latest_file("*.csv")
latest_json = latest_file("run_summary_*.json")
latest_log = latest_log_file()

st.sidebar.subheader("Latest Files")
st.sidebar.caption(f"CSV: {latest_csv.name if latest_csv else 'None yet'}")
st.sidebar.caption(f"HTML: {latest_html.name if latest_html else 'None yet'}")
st.sidebar.caption(f"Summary: {latest_json.name if latest_json else 'None yet'}")
st.sidebar.caption(f"Log: {latest_log.name if latest_log else 'None yet'}")

st.divider()

control_col1, control_col2, control_col3, control_col4 = st.columns(4)

with control_col1:
    st.markdown('<div class="info-card"><b>1. Run Scraper</b><br><span class="small-muted">Scan all active sources and build fresh reports.</span></div>', unsafe_allow_html=True)
    if st.button("Run Scraper", disabled=not admin, use_container_width=True):
        with st.spinner("Running scraper... this may take a few minutes."):
            result = run_scraper()
        if result.returncode == 0:
            st.success("Scraper finished successfully.")
            st.code(result.stdout)
            st.rerun()
        else:
            st.error("Scraper failed.")
            st.code(result.stderr or result.stdout)

with control_col2:
    st.markdown('<div class="info-card"><b>2. Reset Outputs</b><br><span class="small-muted">Delete old database, output files, and logs.</span></div>', unsafe_allow_html=True)
    if st.button("Reset Outputs", disabled=not admin, use_container_width=True):
        reset_outputs()
        st.warning("Database, reports, and logs were deleted.")
        st.rerun()

with control_col3:
    st.markdown('<div class="info-card"><b>3. Create PDF</b><br><span class="small-muted">Generate a clean PDF from the current results.</span></div>', unsafe_allow_html=True)
    if st.button("Create PDF", disabled=not admin, use_container_width=True):
        try:
            csv_file, raw_df = load_latest_csv()
            with st.spinner("Creating PDF..."):
                prepared = prepare_dataframe(raw_df) if raw_df is not None else None
                pdf_path = create_simple_pdf_from_csv(prepared)
            st.success(f"PDF created: {pdf_path.name}")
            st.rerun()
        except Exception as error:
            st.error(f"Could not create PDF: {error}")

with control_col4:
    st.markdown('<div class="info-card"><b>4. Share / Download</b><br><span class="small-muted">Download CSV, HTML, or PDF outputs.</span></div>', unsafe_allow_html=True)
    if PDF_FILE.exists():
        show_download_button("Download Latest PDF", PDF_FILE, "application/pdf")
    else:
        st.button("Download Latest PDF", disabled=True, use_container_width=True)

st.divider()

csv_file, raw_df = load_latest_csv()
summary_data = load_latest_summary()

if raw_df is None:
    st.info("No report found yet. Use the admin controls to run the scraper.")
else:
    df = prepare_dataframe(raw_df)

    st.subheader("Latest Scraper Results")
    st.caption(f"Loaded report: `{csv_file.name}`")

    open_count = int(df["appears_open"].sum())
    priority_count = int(df["has_priority_trade"].sum())
    due_14_count = int(((df["days_until_due"] >= 0) & (df["days_until_due"] <= 14)).sum())
    complete_count = int((df["missing_info"] == "Complete").sum())
    top_score = int(df["score"].max()) if len(df) else 0
    hot_count = int((df["score_band"] == "HOT").sum()) if "score_band" in df.columns else 0

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total Listings", len(df))
    m2.metric("Open / Active", open_count)
    m3.metric("Priority Trade Hits", priority_count)
    m4.metric("Due ≤ 14 Days", due_14_count)
    m5.metric("Complete Info", complete_count)
    m6.metric("Top Score", top_score)

    if summary_data:
        with st.expander("Run summary"):
            st.json(summary_data, expanded=False)

    st.divider()

    filtered_df = filter_dataframe(df)

    st.caption(f"Showing {len(filtered_df)} of {len(df)} listings after filters.")

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
        [
            "Ranked Listings",
            "Priority Trade Board",
            "Upcoming Deadlines",
            "Follow-Up Needed",
            "Report Preview",
            "Downloads & Logs",
        ]
    )

    display_columns = [
        "score_band",
        "score",
        "action_level",
        "priority_trade_matches",
        "title",
        "source_name",
        "county",
        "trade_category",
        "status",
        "days_until_due",
        "due_date_iso",
        "prebid_date_iso",
        "missing_info",
        "contact_info",
        "detail_url",
    ]
    display_columns = [col for col in display_columns if col in filtered_df.columns]

    with tab1:
        st.subheader("Ranked Listings")
        st.dataframe(
            filtered_df[display_columns],
            use_container_width=True,
            hide_index=True,
            column_config={
                "detail_url": st.column_config.LinkColumn("Source Link"),
                "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100),
                "days_until_due": st.column_config.NumberColumn("Days Until Due", format="%d"),
            },
        )

    with tab2:
        st.subheader("Priority Trade Board")
        trade_rows = []
        for trade in PRIORITY_TRADE_ORDER:
            trade_df = filtered_df[filtered_df["priority_trade_match_list"].apply(lambda items: trade in items)]
            if trade_df.empty:
                continue
            trade_rows.append(
                {
                    "Priority Trade": trade,
                    "Listings": len(trade_df),
                    "Open": int(trade_df["appears_open"].sum()),
                    "Top Score": int(trade_df["score"].max()),
                    "Due ≤ 14 Days": int(((trade_df["days_until_due"] >= 0) & (trade_df["days_until_due"] <= 14)).sum()),
                    "Complete Info": int((trade_df["missing_info"] == "Complete").sum()),
                }
            )
        if trade_rows:
            trade_summary_df = pd.DataFrame(trade_rows).sort_values(["Top Score", "Listings"], ascending=[False, False])
            st.dataframe(trade_summary_df, use_container_width=True, hide_index=True)

            selected_trade_focus = st.selectbox(
                "Drill into a priority trade",
                options=[row["Priority Trade"] for row in trade_rows],
            )
            drill_df = filtered_df[filtered_df["priority_trade_match_list"].apply(lambda items: selected_trade_focus in items)]
            st.dataframe(
                drill_df[display_columns],
                use_container_width=True,
                hide_index=True,
                column_config={"detail_url": st.column_config.LinkColumn("Source Link")},
            )
        else:
            st.info("No priority trade matches in the current filtered results.")

    with tab3:
        st.subheader("Upcoming Deadlines")
        deadline_df = filtered_df[
            filtered_df["days_until_due"].notna() & (filtered_df["days_until_due"] >= 0)
        ].sort_values(["days_until_due", "score"], ascending=[True, False])
        if deadline_df.empty:
            st.info("No future due dates found in the filtered results.")
        else:
            st.dataframe(
                deadline_df[display_columns],
                use_container_width=True,
                hide_index=True,
                column_config={"detail_url": st.column_config.LinkColumn("Source Link")},
            )

    with tab4:
        st.subheader("Follow-Up Needed")
        follow_up_df = filtered_df[filtered_df["missing_info"] != "Complete"].sort_values("score", ascending=False)
        if follow_up_df.empty:
            st.success("All filtered listings appear to have the key info this dashboard checks for.")
        else:
            st.caption("These are the listings most worth manually checking for missing due dates, contacts, links, or paperwork.")
            st.dataframe(
                follow_up_df[display_columns],
                use_container_width=True,
                hide_index=True,
                column_config={"detail_url": st.column_config.LinkColumn("Source Link")},
            )

    with tab5:
        st.subheader("Report Preview")
        preview_choice = st.radio("Preview", ["HTML Report", "PDF Report"], horizontal=True)
        if preview_choice == "HTML Report":
            latest_html = latest_file("*.html")
            if latest_html:
                components.html(latest_html.read_text(encoding="utf-8", errors="ignore"), height=850, scrolling=True)
            else:
                st.info("No HTML report found yet.")
        else:
            display_pdf_preview(PDF_FILE)

    with tab6:
        st.subheader("Downloads")
        d1, d2, d3 = st.columns(3)
        with d1:
            show_download_button("Download CSV", csv_file, "text/csv")
        with d2:
            show_download_button("Download HTML Report", latest_file("*.html"), "text/html")
        with d3:
            show_download_button("Download PDF", PDF_FILE if PDF_FILE.exists() else None, "application/pdf")

        st.divider()
        st.subheader("Latest Log")
        log_file = latest_log_file()
        if log_file:
            log_text = log_file.read_text(encoding="utf-8", errors="ignore")
            st.text_area("Log output", log_text[-8000:], height=300)
            show_download_button("Download Log", log_file, "text/plain")
        else:
            st.info("No log file found yet.")
