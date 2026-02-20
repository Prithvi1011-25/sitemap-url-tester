"""
app.py – Streamlit UI for Sitemap URL Tester
==============================================
Upload a sitemap (XML / XML.GZ) or paste a URL, parse all <loc> entries,
check every URL's HTTP status, and view / download results.
"""

import io
import streamlit as st
import pandas as pd

from sitemap import parse_sitemap
from checker import run_checks
from headers import UA_PRESETS

# ──────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sitemap URL Tester",
    page_icon="🌐",
    layout="wide",
)

# ──────────────────────────────────────────────────────────────
# Custom CSS for a polished look
# ──────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* ─── Light theme with black buttons ─── */
    :root { color-scheme: light; }

    .stApp, .main, [data-testid="stAppViewContainer"] {
        background-color: #f8f9fb !important;
        color: #1e293b !important;
    }

    /* Metric cards */
    div[data-testid="stMetric"] {
        background: #ffffff;
        border-radius: 14px;
        padding: 18px 22px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
    }
    div[data-testid="stMetric"] label { color: #64748b !important; font-size: 0.85rem; }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"] { color: #0f172a !important; font-weight: 700; }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background: linear-gradient(180deg, #f1f5f9 0%, #e2e8f0 100%) !important;
    }
    section[data-testid="stSidebar"] .stMarkdown h1,
    section[data-testid="stSidebar"] .stMarkdown h2,
    section[data-testid="stSidebar"] .stMarkdown h3 { color: #1e293b !important; }
    section[data-testid="stSidebar"] .stMarkdown p,
    section[data-testid="stSidebar"] label { color: #334155 !important; }

    /* Primary buttons – BLACK */
    .stButton > button[kind="primary"] {
        background: #09090b !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 10px;
        font-weight: 600;
    }
    .stButton > button[kind="primary"]:hover {
        background: #27272a !important;
    }

    /* Data tables */
    .stDataFrame, [data-testid="stDataFrame"] { border-radius: 10px; overflow: hidden; }

    hr { border-color: #e2e8f0 !important; }

    .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 { color: #0f172a !important; }

    /* Download button – BLACK */
    .stDownloadButton > button {
        background: #09090b !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 10px;
        font-weight: 600;
    }
    .stDownloadButton > button:hover {
        background: #27272a !important;
    }
</style>
""", unsafe_allow_html=True)

# ──────────────────────────────────────────────────────────────
# Title
# ──────────────────────────────────────────────────────────────
st.markdown("# **Sitemap URL Tester**")
st.markdown(
    "Upload a **sitemap.xml** (or `.xml.gz`) or paste a sitemap URL. "
    "The app extracts every URL, checks HTTP status codes asynchronously, "
    "and shows diagnostics you can filter and download as CSV."
)
st.divider()

# ──────────────────────────────────────────────────────────────
# Sidebar – options
# ──────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Options")

    concurrency = st.slider("Concurrency", 1, 50, 10)
    timeout = st.number_input("Timeout (seconds)", min_value=1, max_value=120, value=15)
    follow_redirects = st.checkbox("Follow redirects", value=True)
    head_then_get = st.checkbox("HEAD then GET fallback", value=True)
    retries = st.slider("Retry count", 0, 2, 1)
    safari_retry = st.checkbox("Retry with Safari UA on 403/404", value=True)

    st.divider()
    st.subheader("User-Agent")
    ua_options = list(UA_PRESETS.keys()) + ["Custom"]
    ua_choice = st.selectbox("UA Preset", ua_options, index=0)

    if ua_choice == "Custom":
        user_agent = st.text_input("Custom User-Agent string", value="")
    else:
        user_agent = UA_PRESETS[ua_choice]
        st.caption(f"`{user_agent[:60]}…`")

    st.divider()
    st.subheader("Run Location")
    @st.cache_data(ttl=3600, show_spinner=False)
    def _get_outbound_ip():
        import httpx as _httpx
        try:
            r = _httpx.get("https://ipinfo.io/json", timeout=5)
            data = r.json()
            ip = data.get("ip", "unknown")
            country = data.get("country", "??")
            city = data.get("city", "")
            org = data.get("org", "")
            return f"{ip} — {city}, {country} ({org})"
        except Exception:
            return "Could not detect"

    st.caption(f"Outbound IP: **{_get_outbound_ip()}**")
    st.caption("_If results differ from your browser, your app may be on a different IP/region._")

# ──────────────────────────────────────────────────────────────
# Input section
# ──────────────────────────────────────────────────────────────
col_file, col_url = st.columns(2)

with col_file:
    st.subheader("Upload sitemap file")
    uploaded = st.file_uploader(
        "Choose a sitemap.xml or sitemap.xml.gz",
        type=["xml", "gz"],
        label_visibility="collapsed",
    )

with col_url:
    st.subheader("Or paste sitemap URL")
    sitemap_url = st.text_input(
        "Sitemap URL to fetch",
        placeholder="https://example.com/sitemap.xml",
        label_visibility="collapsed",
    )

# ──────────────────────────────────────────────────────────────
# Session state
# ──────────────────────────────────────────────────────────────
if "urls" not in st.session_state:
    st.session_state.urls = []
if "results_df" not in st.session_state:
    st.session_state.results_df = None

# ──────────────────────────────────────────────────────────────
# Parse sitemap
# ──────────────────────────────────────────────────────────────
st.divider()

if st.button("Parse Sitemap", type="primary", width="stretch"):
    with st.spinner("Parsing sitemap…"):
        parse_log = st.empty()

        def _log(msg: str) -> None:
            parse_log.info(msg)

        if uploaded is not None:
            raw = uploaded.read()
            urls = parse_sitemap(raw, user_agent=user_agent, progress_callback=_log)
        elif sitemap_url.strip():
            urls = parse_sitemap(sitemap_url.strip(), user_agent=user_agent, progress_callback=_log)
        else:
            st.warning("Please upload a file **or** enter a URL first.")
            urls = []

        st.session_state.urls = urls
        st.session_state.results_df = None
        parse_log.empty()

    if st.session_state.urls:
        st.success(f"Found **{len(st.session_state.urls)}** unique URLs.")
    elif uploaded or sitemap_url.strip():
        st.error("No URLs found – check the sitemap source.")

# Show parsed URLs preview
if st.session_state.urls:
    with st.expander(f"Parsed URL list ({len(st.session_state.urls)} URLs)", expanded=False):
        st.dataframe(
            pd.DataFrame({"url": st.session_state.urls}),
            width="stretch",
            height=300,
        )

# ──────────────────────────────────────────────────────────────
# Run checks
# ──────────────────────────────────────────────────────────────
if st.session_state.urls:
    st.divider()
    if st.button("Run Checks", type="primary", width="stretch"):
        progress_bar = st.progress(0, text="Starting checks…")

        def _progress(done: int, total: int) -> None:
            pct = done / total if total else 1.0
            progress_bar.progress(pct, text=f"Checking URLs… {done}/{total}")

        results = run_checks(
            st.session_state.urls,
            concurrency=concurrency,
            timeout=float(timeout),
            follow_redirects=follow_redirects,
            head_then_get=head_then_get,
            retries=retries,
            user_agent=user_agent,
            safari_retry=safari_retry,
            progress_callback=_progress,
        )

        progress_bar.progress(1.0, text="Done")
        st.session_state.results_df = pd.DataFrame(results)

# ──────────────────────────────────────────────────────────────
# Results display
# ──────────────────────────────────────────────────────────────
if st.session_state.results_df is not None and not st.session_state.results_df.empty:
    df: pd.DataFrame = st.session_state.results_df.copy()

    st.divider()
    st.subheader("Results Summary")

    # ── Classify status groups based on final_status_code ──
    status_col = "final_status_code" if "final_status_code" in df.columns else "status_code"

    def _group(code: str) -> str:
        if code.isdigit():
            first = code[0]
            return {"2": "2xx", "3": "3xx", "4": "4xx", "5": "5xx"}.get(first, "Other")
        return "Error"

    df["status_group"] = df[status_col].apply(_group)

    # Count redirecting URLs (final is 200 but first_status was 3xx)
    redirecting = 0
    if "first_status_code" in df.columns:
        redirecting = int(
            ((df["first_status_code"].str.startswith("3")) & (df[status_col] == "200")).sum()
        )

    total = len(df)
    g2 = int((df["status_group"] == "2xx").sum())
    g3 = int((df["status_group"] == "3xx").sum())
    g4 = int((df["status_group"] == "4xx").sum())
    g5 = int((df["status_group"] == "5xx").sum())
    errs = int((df["status_group"] == "Error").sum())
    soft_404_count = int(df["soft_404"].sum()) if "soft_404" in df.columns else 0

    row1 = st.columns(4)
    row1[0].metric("Total", total)
    row1[1].metric("2xx", g2)
    row1[2].metric("Redirecting", redirecting)
    row1[3].metric("3xx (final)", g3)

    row2 = st.columns(4)
    row2[0].metric("4xx", g4)
    row2[1].metric("5xx", g5)
    row2[2].metric("Errors", errs)
    row2[3].metric("Soft 404", soft_404_count)

    # ── Error breakdown table ──
    if errs > 0 and "error" in df.columns:
        st.divider()
        st.subheader("Error Breakdown")
        error_df = df[df["error"] != ""]
        breakdown = error_df["error"].value_counts().reset_index()
        breakdown.columns = ["Error Type", "Count"]
        st.dataframe(breakdown, use_container_width=True, height=min(200, 35 * len(breakdown) + 50))

    # ── Filters ──
    st.divider()
    st.subheader("Filter & Sort Results")

    fcol1, fcol2, fcol3 = st.columns([1, 1, 2])
    with fcol1:
        group_filter = st.selectbox(
            "Status group",
            ["All", "2xx", "3xx", "4xx", "5xx", "Error"],
        )
    with fcol2:
        unique_codes = sorted(df[status_col].unique().tolist())
        code_filter = st.selectbox(
            "Exact status code",
            ["All"] + unique_codes,
        )
    with fcol3:
        search = st.text_input("Search URL substring", "")

    # Sort options
    sort_col1, sort_col2 = st.columns([1, 1])
    with sort_col1:
        sort_by = st.selectbox(
            "Sort by",
            ["Original order", "Status code", "Response time (ms)", "Redirect count"],
        )
    with sort_col2:
        sort_order = st.radio("Order", ["Ascending", "Descending"], horizontal=True)

    filtered = df.copy()

    # Apply group filter
    if group_filter != "All":
        filtered = filtered[filtered["status_group"] == group_filter]

    # Apply individual status code filter
    if code_filter != "All":
        filtered = filtered[filtered[status_col] == code_filter]

    # Apply search
    if search.strip():
        mask = (
            filtered["input_url"].str.contains(search, case=False, na=False)
            | filtered["final_url"].str.contains(search, case=False, na=False)
        )
        filtered = filtered[mask]

    # Apply sort
    ascending = sort_order == "Ascending"
    sort_map = {
        "Status code": status_col,
        "Response time (ms)": "response_time_ms",
        "Redirect count": "redirect_count",
    }
    if sort_by in sort_map:
        filtered = filtered.sort_values(sort_map[sort_by], ascending=ascending)

    # Build display columns
    display_cols = [
        "input_url", "first_status_code", "final_status_code", "final_url",
        "response_time_ms", "redirect_count", "redirect_chain",
        "method_used", "user_agent_used", "soft_404",
        "error", "status_group",
    ]
    display_cols = [c for c in display_cols if c in filtered.columns]

    st.dataframe(filtered[display_cols], use_container_width=True, height=500)
    st.caption(f"Showing {len(filtered)} of {total} URLs")

    # ── Downloads ──
    st.divider()
    dl_col1, dl_col2 = st.columns(2)
    with dl_col1:
        csv_all = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download All Results CSV",
            data=csv_all,
            file_name="sitemap_check_all.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with dl_col2:
        failures_df = df[df["status_group"].isin(["4xx", "5xx", "Error"]) | (df.get("soft_404", False) == True)]
        csv_fail = failures_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            f"Download Failures Only CSV ({len(failures_df)})",
            data=csv_fail,
            file_name="sitemap_check_failures.csv",
            mime="text/csv",
            use_container_width=True,
        )
