import os
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st
import reportlab

BASE_DIR = Path(__file__).parent
SCRAPER_FILE = BASE_DIR / "nj_bid_scraper.py"
CONFIG_FILE = BASE_DIR / "config" / "sources.yaml"
DB_FILE = BASE_DIR / "data" / "bids.sqlite"
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"
PDF_FILE = OUTPUT_DIR / "bid_report_latest.pdf"


st.set_page_config(
    page_title="NJ Bid Scraper Dashboard",
    page_icon="🏗️",
    layout="wide",
)


def get_admin_password():
    """
    Gets the admin password from Streamlit secrets if deployed,
    otherwise uses a local fallback password.
    """
    try:
        return st.secrets.get("ADMIN_PASSWORD", "change-this-password")
    except Exception:
        return "change-this-password"


def is_admin():
    st.sidebar.header("Admin Access")
    password = st.sidebar.text_input("Admin password", type="password")
    return password == get_admin_password()


def latest_file(pattern):
    OUTPUT_DIR.mkdir(exist_ok=True)

    files = list(OUTPUT_DIR.glob(pattern))

    if not files:
        return None

    return max(files, key=lambda file: file.stat().st_mtime)


def reset_outputs():
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


def run_scraper():
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

    result = subprocess.run(
        command,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )

    return result


def load_latest_csv():
    csv_file = latest_file("*.csv")

    if csv_file is None:
        return None, None

    df = pd.read_csv(csv_file)
    return csv_file, df


def create_simple_pdf_from_csv():
    """
    Creates a simple PDF directly from the latest CSV report.

    This avoids the browser Ctrl+P / 0-byte PDF problem.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    except ImportError as exc:
        raise RuntimeError(
            "ReportLab is required to create PDF reports. Install it with 'pip install reportlab'."
        ) from exc

    csv_file, df = load_latest_csv()

    if csv_file is None or df is None:
        raise FileNotFoundError("No CSV report found. Run the scraper first.")

    OUTPUT_DIR.mkdir(exist_ok=True)

    doc = SimpleDocTemplate(
        str(PDF_FILE),
        pagesize=landscape(letter),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )

    styles = getSampleStyleSheet()
    story = []

    title = Paragraph("NJ Public Bid Scraper Report", styles["Title"])
    story.append(title)
    story.append(Spacer(1, 12))

    story.append(Paragraph(f"Source CSV: {csv_file.name}", styles["Normal"]))
    story.append(Spacer(1, 12))

    display_columns = []

    preferred_columns = [
        "score_band",
        "score",
        "trade_priority",
        "title",
        "source_name",
        "county",
        "trade_category",
        "due_date_iso",
        "detail_url",
    ]

    for col in preferred_columns:
        if col in df.columns:
            display_columns.append(col)

    if not display_columns:
        display_columns = list(df.columns[:8])

    pdf_df = df[display_columns].copy()

    # Limit very long text so the PDF stays readable.
    for col in pdf_df.columns:
        pdf_df[col] = pdf_df[col].astype(str).str.slice(0, 80)

    table_data = [list(pdf_df.columns)] + pdf_df.values.tolist()

    table = Table(table_data, repeatRows=1)

    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ]
        )
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
            )


st.title("🏗️ NJ Public Bid Scraper Dashboard")
st.caption("Scans public NJ bidding websites, scores listings, and produces organized reports.")

admin = is_admin()

if not admin:
    st.sidebar.warning("Enter the admin password to run or reset the scraper.")
else:
    st.sidebar.success("Admin access granted.")

st.divider()

col1, col2, col3, col4 = st.columns(4)

with col1:
    if st.button("Run Scraper", disabled=not admin):
        with st.spinner("Running scraper... this may take a few minutes."):
            result = run_scraper()

        if result.returncode == 0:
            st.success("Scraper finished successfully.")
            st.code(result.stdout)
        else:
            st.error("Scraper failed.")
            st.code(result.stderr)

with col2:
    if st.button("Reset Outputs", disabled=not admin):
        reset_outputs()
        st.warning("Database, reports, and logs were deleted.")

with col3:
    if st.button("Create PDF", disabled=not admin):
        try:
            with st.spinner("Creating PDF..."):
                pdf_path = create_simple_pdf_from_csv()

            st.success(f"PDF created: {pdf_path.name}")
        except Exception as error:
            st.error(f"Could not create PDF: {error}")

with col4:
    latest_html = latest_file("*.html")

    if latest_html:
        st.link_button("Open HTML Report", latest_html.resolve().as_uri())
    else:
        st.button("Open HTML Report", disabled=True)


st.divider()

csv_file, df = load_latest_csv()

if df is None:
    st.info("No report found yet. Use the admin controls to run the scraper.")
else:
    st.subheader("Latest Scraper Results")
    st.write(f"Loaded report: `{csv_file.name}`")

    metric1, metric2, metric3, metric4 = st.columns(4)

    with metric1:
        st.metric("Total Listings", len(df))

    with metric2:
        if "score" in df.columns and len(df) > 0:
            st.metric("Top Score", int(df["score"].max()))
        else:
            st.metric("Top Score", "N/A")

    with metric3:
        if "score_band" in df.columns:
            hot_count = int((df["score_band"] == "HOT").sum())
            st.metric("HOT Listings", hot_count)
        else:
            st.metric("HOT Listings", "N/A")

    with metric4:
        if "trade_priority" in df.columns:
            primary_count = int((df["trade_priority"] == "Primary - High Margin").sum())
            st.metric("High-Margin Matches", primary_count)
        else:
            st.metric("High-Margin Matches", "N/A")

    st.divider()

    st.subheader("Filter Results")

    filtered_df = df.copy()

    filter_col1, filter_col2, filter_col3 = st.columns(3)

    with filter_col1:
        if "score_band" in df.columns:
            selected_bands = st.multiselect(
                "Score Band",
                options=sorted(df["score_band"].dropna().unique()),
                default=sorted(df["score_band"].dropna().unique()),
            )

            filtered_df = filtered_df[filtered_df["score_band"].isin(selected_bands)]

    with filter_col2:
        if "county" in df.columns:
            selected_counties = st.multiselect(
                "County",
                options=sorted(df["county"].dropna().unique()),
                default=sorted(df["county"].dropna().unique()),
            )

            filtered_df = filtered_df[filtered_df["county"].isin(selected_counties)]

    with filter_col3:
        if "trade_priority" in df.columns:
            selected_priorities = st.multiselect(
                "Trade Priority",
                options=sorted(df["trade_priority"].dropna().unique()),
                default=sorted(df["trade_priority"].dropna().unique()),
            )

            filtered_df = filtered_df[filtered_df["trade_priority"].isin(selected_priorities)]

    st.subheader("Ranked Listings")
    st.dataframe(filtered_df, use_container_width=True)

    st.divider()

    st.subheader("Downloads")

    download_col1, download_col2, download_col3 = st.columns(3)

    with download_col1:
        show_download_button(
            "Download CSV",
            csv_file,
            "text/csv",
        )

    with download_col2:
        latest_html = latest_file("*.html")

        if latest_html:
            show_download_button(
                "Download HTML Report",
                latest_html,
                "text/html",
            )

    with download_col3:
        if PDF_FILE.exists():
            show_download_button(
                "Download PDF",
                PDF_FILE,
                "application/pdf",
            )
        else:
            st.info("Create a PDF first.")