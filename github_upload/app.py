import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st


BASE_DIR = Path(__file__).parent
SCRAPER_FILE = BASE_DIR / "nj_bid_scraper.py"
CONFIG_FILE = BASE_DIR / "config" / "sources.yaml"
TRADE_RULES_FILE = BASE_DIR / "config" / "trade_rules.yaml"
AREAS_FILE = BASE_DIR / "config" / "areas.yaml"
DB_FILE = BASE_DIR / "data" / "bids.sqlite"
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"
PDF_FILE = OUTPUT_DIR / "bid_report_latest.pdf"


st.set_page_config(
    page_title="NJ Bid Intelligence Dashboard",
    page_icon="🏗️",
    layout="wide",
)


CUSTOM_CSS = """
<style>
.block-container {padding-top: 1.5rem; padding-bottom: 2rem;}
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    padding: 14px;
    border-radius: 14px;
    box-shadow: 0 1px 2px rgba(0,0,0,.04);
}
div[data-testid="stButton"] > button {
    border-radius: 10px;
    height: 2.8rem;
}
.small-muted {color: #667085; font-size: 0.9rem;}
.card {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 14px;
    padding: 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,.04);
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


def get_admin_password() -> str:
    try:
        return st.secrets.get("ADMIN_PASSWORD", "change-this-password")
    except Exception:
        return "change-this-password"


def is_admin() -> bool:
    st.sidebar.header("Admin Access")
    password = st.sidebar.text_input("Admin password", type="password")
    return password == get_admin_password()


def load_yaml_safe(path: Path) -> dict:
    import yaml
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_sources_df() -> pd.DataFrame:
    cfg = load_yaml_safe(CONFIG_FILE)
    rows = []
    for s in cfg.get("sources", []):
        rows.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "county": s.get("county"),
            "municipality": s.get("municipality") or "",
            "source_type": s.get("source_type"),
            "scraper_type": s.get("scraper_type"),
            "source_quality": s.get("source_quality"),
            "active": bool(s.get("active", True)),
            "url": s.get("url"),
        })
    return pd.DataFrame(rows)


def latest_file(pattern: str):
    OUTPUT_DIR.mkdir(exist_ok=True)
    files = list(OUTPUT_DIR.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda file: file.stat().st_mtime)


def reset_outputs():
    DB_FILE.unlink(missing_ok=True)
    for folder in [OUTPUT_DIR, LOGS_DIR]:
        folder.mkdir(exist_ok=True)
        for item in folder.iterdir():
            if item.is_file():
                item.unlink()


def run_scraper(counties, municipalities, source_types, include_statewide, max_sources=None):
    OUTPUT_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    DB_FILE.parent.mkdir(exist_ok=True)

    command = [
        sys.executable,
        str(SCRAPER_FILE),
        "run",
        "--config",
        str(CONFIG_FILE),
        "--trade-rules",
        str(TRADE_RULES_FILE),
        "--areas",
        str(AREAS_FILE),
        "--db",
        str(DB_FILE),
        "--out",
        str(OUTPUT_DIR),
    ]

    if counties:
        command += ["--counties", ",".join(counties)]
    if municipalities:
        command += ["--municipalities", ",".join(municipalities)]
    if source_types:
        command += ["--source-types", ",".join(source_types)]
    if include_statewide:
        command += ["--include-statewide"]
    if max_sources:
        command += ["--max-sources", str(max_sources)]

    result = subprocess.run(
        command,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )

    return result


def load_latest_csv():
    csv_file = latest_file("bid_report_*.csv")
    if csv_file is None:
        return None, None
    df = pd.read_csv(csv_file)
    return csv_file, df


def load_latest_coverage():
    coverage_file = latest_file("coverage_report_*.csv")
    if coverage_file is None:
        return None, None
    df = pd.read_csv(coverage_file)
    return coverage_file, df


def safe_text_column(df: pd.DataFrame, column_name: str) -> pd.Series:
    if column_name in df.columns:
        return df[column_name].fillna("").astype(str)
    return pd.Series([""] * len(df), index=df.index, dtype="object")


def prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ["score", "confidence_score", "information_completeness", "document_count", "construction_relevance"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    if "due_date_iso" in df.columns:
        due_dt = pd.to_datetime(df["due_date_iso"], errors="coerce")
        df["days_until_due"] = (due_dt.dt.date - pd.Timestamp.today().date()).apply(lambda x: x.days if pd.notna(x) else None)
        df["is_open_date"] = df["days_until_due"].apply(lambda x: True if x is not None and x >= 0 else False)
    else:
        df["days_until_due"] = None
        df["is_open_date"] = False

    for col in ["matched_trades", "missing_fields", "score_reasons", "linked_documents"]:
        if col not in df.columns:
            df[col] = ""

    df["has_contact"] = safe_text_column(df, "contact_info").str.len() > 4
    df["has_link"] = safe_text_column(df, "detail_url").str.startswith("http")
    doc_text = safe_text_column(df, "linked_documents") + " " + safe_text_column(df, "linked_documents_json")
    df["has_documents"] = doc_text.str.len() > 4
    df["missing_fields_count"] = safe_text_column(df, "missing_fields").apply(lambda x: 0 if not x else len([p for p in str(x).split(";") if p.strip()]))

    return df


def create_simple_pdf_from_csv():
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    except ImportError as exc:
        raise RuntimeError("ReportLab is required. Install it with pip install reportlab.") from exc

    csv_file, df = load_latest_csv()
    if csv_file is None or df is None:
        raise FileNotFoundError("No CSV report found. Run the scraper first.")

    df = prepare_dataframe(df)
    OUTPUT_DIR.mkdir(exist_ok=True)

    doc = SimpleDocTemplate(
        str(PDF_FILE),
        pagesize=landscape(letter),
        rightMargin=18,
        leftMargin=18,
        topMargin=18,
        bottomMargin=18,
    )

    styles = getSampleStyleSheet()
    story = [
        Paragraph("NJ Public Bid Intelligence Report", styles["Title"]),
        Spacer(1, 8),
        Paragraph(f"Source CSV: {csv_file.name}", styles["Normal"]),
        Spacer(1, 10),
    ]

    preferred_columns = [
        "score_band", "score", "action_level", "confidence_score", "title", "county",
        "municipality", "trade_category", "trade_tier", "status", "due_date_iso", "detail_url",
    ]
    display_columns = [c for c in preferred_columns if c in df.columns]
    if not display_columns:
        display_columns = list(df.columns[:10])

    pdf_df = df[display_columns].copy().head(80)
    for col in pdf_df.columns:
        pdf_df[col] = pdf_df[col].astype(str).str.slice(0, 70)

    table_data = [list(pdf_df.columns)] + pdf_df.values.tolist()
    table = Table(table_data, repeatRows=1)
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 6.5),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ])
    )
    story.append(table)
    doc.build(story)
    return PDF_FILE


def show_download_button(label, file_path, mime_type):
    if file_path and Path(file_path).exists():
        with open(file_path, "rb") as file:
            st.download_button(
                label=label,
                data=file,
                file_name=Path(file_path).name,
                mime=mime_type,
                use_container_width=True,
            )


def normalize_unique(series: pd.Series):
    return sorted([x for x in series.dropna().astype(str).unique() if x and x.lower() != "nan"])


st.title("🏗️ NJ Public Bid Intelligence Dashboard")
st.caption("Scans configured state, county, and municipal bid sources, ranks opportunities, and highlights contractor follow-up needs.")

admin = is_admin()
if not admin:
    st.sidebar.warning("Enter the admin password to run, reset, or create PDFs.")
else:
    st.sidebar.success("Admin access granted.")

sources_df = load_sources_df()
active_sources = sources_df[sources_df["active"] == True].copy() if not sources_df.empty else pd.DataFrame()

st.sidebar.divider()
st.sidebar.header("Run Scope")

county_options = normalize_unique(active_sources["county"]) if not active_sources.empty else []
county_options = [c for c in county_options if c.lower() != "statewide"] + (["Statewide"] if "Statewide" in county_options else [])

selected_counties = st.sidebar.multiselect(
    "Counties to scan",
    options=[c for c in county_options if c != "Statewide"],
    default=[c for c in county_options if c != "Statewide"],
)

mun_pool = active_sources.copy()
if selected_counties and "county" in mun_pool.columns:
    mun_pool = mun_pool[mun_pool["county"].isin(selected_counties)]
mun_options = normalize_unique(mun_pool["municipality"]) if not mun_pool.empty and "municipality" in mun_pool.columns else []

selected_municipalities = st.sidebar.multiselect(
    "Municipalities to focus on",
    options=mun_options,
    default=[],
    help="Leave blank to scan all configured municipalities within the selected counties.",
)

source_type_options = normalize_unique(active_sources["source_type"]) if not active_sources.empty else []
selected_source_types = st.sidebar.multiselect(
    "Source types",
    options=source_type_options,
    default=source_type_options,
)

include_statewide = st.sidebar.checkbox("Include statewide sources", value=True)
test_mode = st.sidebar.checkbox("Test mode: limit sources", value=False)
max_sources = st.sidebar.number_input("Max sources", min_value=1, max_value=200, value=5) if test_mode else None

st.divider()

col1, col2, col3, col4 = st.columns(4)

with col1:
    if st.button("🚀 Run Scraper", disabled=not admin, use_container_width=True):
        with st.spinner("Running scraper across selected county/municipality sources..."):
            result = run_scraper(selected_counties, selected_municipalities, selected_source_types, include_statewide, max_sources)
        if result.returncode == 0:
            st.success("Scraper finished successfully.")
            st.code(result.stdout)
        else:
            st.error("Scraper failed.")
            st.code(result.stderr)

with col2:
    if st.button("🧹 Reset Outputs", disabled=not admin, use_container_width=True):
        reset_outputs()
        st.warning("Database, reports, and logs were deleted.")

with col3:
    if st.button("📄 Create PDF", disabled=not admin, use_container_width=True):
        try:
            with st.spinner("Creating PDF..."):
                pdf_path = create_simple_pdf_from_csv()
            st.success(f"PDF created: {pdf_path.name}")
        except Exception as error:
            st.error(f"Could not create PDF: {error}")

with col4:
    latest_html = latest_file("bid_report_*.html")
    if latest_html and latest_html.exists():
        html_text = latest_html.read_text(encoding="utf-8", errors="ignore")
        with st.popover("🌐 Preview HTML Report", use_container_width=True):
            st.components.v1.html(html_text, height=700, scrolling=True)
    else:
        st.button("🌐 Preview HTML Report", disabled=True, use_container_width=True)

csv_file, raw_df = load_latest_csv()

if raw_df is None:
    st.info("No report found yet. Enter the admin password, choose a run scope, then click Run Scraper.")
else:
    df = prepare_dataframe(raw_df)

    st.subheader("Latest Results")
    st.write(f"Loaded report: `{csv_file.name}`")

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    with m1:
        st.metric("Total Listings", len(df))
    with m2:
        st.metric("Top Score", int(df["score"].max()) if "score" in df.columns and len(df) else "N/A")
    with m3:
        st.metric("HOT", int((df["score_band"] == "HOT").sum()) if "score_band" in df.columns else "N/A")
    with m4:
        st.metric("Review for Bid", int((df["action_level"] == "Review for Bid").sum()) if "action_level" in df.columns else "N/A")
    with m5:
        high_count = int((df["trade_tier"] == "High Priority").sum()) if "trade_tier" in df.columns else 0
        st.metric("High Priority", high_count)
    with m6:
        due_soon = int(df["days_until_due"].apply(lambda x: x is not None and 0 <= x <= 21).sum())
        st.metric("Due ≤21 Days", due_soon)

    tab_results, tab_trades, tab_followup, tab_coverage, tab_downloads = st.tabs([
        "Ranked Listings",
        "Priority Trade Board",
        "Follow-Up Needed",
        "Source Coverage",
        "Downloads & Logs",
    ])

    with tab_results:
        st.subheader("Filter Results")
        filtered = df.copy()

        f1, f2, f3, f4 = st.columns(4)
        with f1:
            if "score_band" in filtered.columns:
                bands = st.multiselect("Score Band", normalize_unique(df["score_band"]), default=normalize_unique(df["score_band"]))
                filtered = filtered[filtered["score_band"].isin(bands)]
        with f2:
            if "county" in filtered.columns:
                counties = st.multiselect("County", normalize_unique(df["county"]), default=normalize_unique(df["county"]))
                filtered = filtered[filtered["county"].isin(counties)]
        with f3:
            if "municipality" in filtered.columns:
                municipalities = st.multiselect("Municipality", normalize_unique(df["municipality"]), default=normalize_unique(df["municipality"]))
                filtered = filtered[filtered["municipality"].isin(municipalities)]
        with f4:
            if "trade_category" in filtered.columns:
                trades = st.multiselect("Trade", normalize_unique(df["trade_category"]), default=normalize_unique(df["trade_category"]))
                filtered = filtered[filtered["trade_category"].isin(trades)]

        f5, f6, f7, f8 = st.columns(4)
        with f5:
            min_score = st.slider("Minimum Score", 0, 100, 0)
            filtered = filtered[filtered["score"] >= min_score]
        with f6:
            open_only = st.checkbox("Open / future due date only", value=False)
            if open_only:
                filtered = filtered[filtered["is_open_date"] == True]
        with f7:
            complete_only = st.checkbox("Complete info only", value=False)
            if complete_only:
                filtered = filtered[(filtered["has_contact"]) & (filtered["has_link"]) & (filtered["has_documents"])]
        with f8:
            search = st.text_input("Search text")
            if search:
                search_cols = ["title", "summary", "source_name", "trade_category", "matched_trades"]
                haystack = pd.Series([""] * len(filtered), index=filtered.index)
                for col in search_cols:
                    if col in filtered.columns:
                        haystack = haystack + " " + filtered[col].fillna("").astype(str)
                filtered = filtered[haystack.str.contains(search, case=False, na=False)]

        st.subheader("Ranked Listings")
        preferred_cols = [
            "score_band", "score", "action_level", "confidence_score", "information_completeness",
            "title", "county", "municipality", "trade_category", "matched_trades",
            "trade_tier", "status", "due_date_iso", "prebid_date_iso", "contact_info",
            "document_count", "detail_url", "summary",
        ]
        show_cols = [c for c in preferred_cols if c in filtered.columns]
        st.dataframe(filtered[show_cols], use_container_width=True, height=620)

    with tab_trades:
        st.subheader("Priority Trade Board")
        if "trade_category" in df.columns:
            board = (
                df.groupby(["trade_category", "trade_tier"], dropna=False)
                .agg(
                    listings=("title", "count"),
                    top_score=("score", "max"),
                    avg_score=("score", "mean"),
                    review_now=("action_level", lambda s: int((s == "Review for Bid").sum())),
                    with_contact=("has_contact", "sum"),
                    with_docs=("has_documents", "sum"),
                )
                .reset_index()
                .sort_values(["trade_tier", "top_score"], ascending=[True, False])
            )
            board["avg_score"] = board["avg_score"].round(1)
            st.dataframe(board, use_container_width=True)
        else:
            st.info("No trade_category column found.")

    with tab_followup:
        st.subheader("Follow-Up Needed")
        followup = df.copy()
        if "missing_fields" in followup.columns:
            followup = followup[(followup["missing_fields"].fillna("").astype(str).str.len() > 0) | (followup["action_level"].isin(["Follow Up", "Review for Bid"]))]
        followup_cols = [
            "score", "action_level", "title", "county", "municipality", "trade_category",
            "due_date_iso", "missing_fields", "contact_info", "detail_url", "summary",
        ]
        followup_cols = [c for c in followup_cols if c in followup.columns]
        st.dataframe(followup[followup_cols], use_container_width=True, height=520)

    with tab_coverage:
        st.subheader("Configured Source Coverage")
        st.caption("This shows which county/municipality sources are configured. Missing towns need a public bid URL added to config/sources.yaml.")

        cfile, cdf = load_latest_coverage()
        if cdf is not None:
            st.write(f"Loaded coverage report: `{cfile.name}`")
            st.dataframe(cdf, use_container_width=True, height=520)
        else:
            st.info("Coverage report will appear after the scraper runs.")

        st.subheader("Active Source Registry")
        if not active_sources.empty:
            st.dataframe(active_sources, use_container_width=True, height=400)

    with tab_downloads:
        st.subheader("Downloads")
        d1, d2, d3, d4 = st.columns(4)
        with d1:
            show_download_button("Download CSV", csv_file, "text/csv")
        with d2:
            latest_html = latest_file("bid_report_*.html")
            show_download_button("Download HTML", latest_html, "text/html")
        with d3:
            show_download_button("Download PDF", PDF_FILE, "application/pdf")
        with d4:
            latest_json = latest_file("run_summary_*.json")
            show_download_button("Download JSON", latest_json, "application/json")

        st.subheader("Latest Logs")
        log_file = latest_file("scraper_*.log") if LOGS_DIR.exists() else None
        if log_file and log_file.exists():
            st.text_area("Log", log_file.read_text(encoding="utf-8", errors="ignore")[-8000:], height=300)
        else:
            st.info("No logs found yet.")
