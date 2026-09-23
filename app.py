"""Streamlit UI: uv run streamlit run app.py  ->  http://localhost:8501"""

from pathlib import Path

import streamlit as st

from b144.runner import DEFAULT_CONCURRENCY, MAX_CONCURRENCY, Runner

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

st.set_page_config(page_title="B144 Scraper", page_icon="📇", layout="wide")


@st.cache_resource
def get_runner() -> Runner:
    # One runner per server process; its background thread survives page reruns and closed tabs.
    return Runner()


R = get_runner()
store = R.store


def fmt_duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def download_button(path: str, label: str, key: str):
    p = Path(path)
    if p.exists():
        st.download_button(label, data=lambda: p.read_bytes(), file_name=p.name, mime=XLSX_MIME, key=key,
                           type="primary", icon=":material/download:")
        st.caption(f"`{p}` · {p.stat().st_size / 1_048_576:.1f} MB")


# ---- sidebar -------------------------------------------------------------------------------------
running = R.is_running()
with st.sidebar:
    st.header("Settings")
    st.caption("These settings apply to the next **Run**. A resumed run keeps the settings it started with.")
    cats = store.categories()
    labels = {c["cat_code"]: f"{c['name']}  ·  {c['cat_code']}" for c in cats}
    all_cats = st.toggle("All categories", value=True, disabled=running)
    selected = st.multiselect("Categories", options=list(labels), format_func=labels.get,
                              disabled=all_cats or running, placeholder="Type to search categories…")
    if cats:
        st.caption(f"{len(cats):,} categories · list built {store.get_meta('categories_built_at', '?')}")
    else:
        st.caption("The category list hasn't been built yet. It's built automatically on the first Run "
                   "(about 2 minutes), or click **Refresh category list**.")
    concurrency = st.slider("Concurrent requests", 1, MAX_CONCURRENCY, DEFAULT_CONCURRENCY, disabled=running,
                            help="How many requests run at the same time. Listings and business details share "
                                 "them, and several categories are worked on at once. Higher is faster but makes "
                                 "the site more likely to block the scraper; if it slows you down (403/429), "
                                 "every worker pauses together.")
    delay = st.slider("Random delay per request (s)", 0.0, 3.0, (0.3, 0.8), step=0.1, disabled=running)
    if st.button("Refresh category list", disabled=running, width="stretch"):
        R.refresh_categories(concurrency, delay)
        st.rerun()

# ---- controls ----------------------------------------------------------------------------------------
st.title("B144 business scraper")
st.caption("Every category on b144.co.il, across all 15 regions of the country, into one Excel file.")

unfinished = None if running else store.unfinished_run()
buttons = st.container(horizontal=True)
if buttons.button("Run", type="primary", icon=":material/play_arrow:", disabled=running):
    if not all_cats and not selected:
        st.error("Pick at least one category, or switch on **All categories**.")
    else:
        R.start(None if all_cats else selected, True, concurrency, tuple(delay), city_sweep=True)
        st.rerun()
if buttons.button("Stop", icon=":material/stop:", disabled=not running):
    R.stop()
    st.rerun()
if buttons.button("Resume", icon=":material/replay:", disabled=running or not unfinished):
    R.resume()
    st.rerun()

if unfinished:
    done = store.counters(unfinished["id"])
    st.warning(f"Unfinished run found — Resume. Run #{unfinished['id']} ({unfinished['status']}), started "
               f"{unfinished['started_at']}: {done['cats_done']}/{done['cats_total']} categories, "
               f"{done['listings']:,} rows so far. {unfinished.get('note') or ''}  \n"
               "Pressing **Run** instead starts a fresh run and discards this one's progress.",
               icon=":material/pause_circle:")


# ---- live progress (refreshes every 2 s) -------------------------------------------------------------
@st.fragment(run_every=2)
def progress_panel():
    s = R.status()
    # When the runner starts/stops on its own, rerun the whole page so the buttons update.
    if st.session_state.get("_was_running") != s["running"]:
        first = "_was_running" not in st.session_state
        st.session_state["_was_running"] = s["running"]
        if not first:
            st.rerun(scope="app")

    run = s.get("run")
    phase = s["phase"]
    if run:
        scope = s.get("scope") or {}
        what = (f"all {scope.get('count', 0):,} categories" if run.get("all_categories")
                else f"{scope.get('count', 0):,} categor{'y' if scope.get('count') == 1 else 'ies'}")
        if not run.get("all_categories") and scope.get("names") and scope.get("count", 0) <= 5:
            what += ": " + ", ".join(scope["names"])
        st.caption(f"Run #{run['id']} · {what}")
    if s["running"]:
        if phase == "scraping":
            exp = s.get("listings_expected") or 0
            frac = s["work_done"] / s["work_expected"] if s.get("work_expected") else 0  # listings + details
            text = (f"Scraping — {s['cats_done']:,}/{s['cats_total']:,} categories · "
                    f"~{s['listings_done']:,} of ~{exp:,} listings")
        elif phase == "details":
            frac = s["details_done"] / s["businesses"] if s.get("businesses") else 0
            text = f"Fetching business details — {s['details_done']:,}/{s['businesses']:,}"
        elif phase == "export":
            frac, text = 1.0, "Writing Excel…"
        else:
            frac, text = 0.0, s.get("message") or "Starting…"
        st.progress(min(1.0, frac), text=text)
        if s.get("message") and phase in ("scraping", "categories"):
            st.caption(s["message"])
    elif run:
        st.info(f"Last run #{run['id']}: **{run['status']}**"
                + (f" · finished {run['finished_at']}" if run.get("finished_at") else "")
                + (f" · {run['note']}" if run.get("note") else ""), icon=":material/info:")
    else:
        st.info("No runs yet. Pick categories (or leave **All categories** on) and press **Run**.")

    if run:
        m = st.columns(3) + st.columns(3)
        m[0].metric("Categories done", f"{s['cats_done']:,}/{s['cats_total']:,}")
        m[1].metric("Listings found", f"{s['listings']:,}")
        m[2].metric("Unique businesses", f"{s['businesses']:,}")
        m[3].metric("Details fetched", f"{s['details_done']:,}")
        m[4].metric("Errors", f"{s['errors']:,}")
        m[5].metric("Time left (estimate)", fmt_duration(s.get("eta")) if s["running"] and s.get("eta") else
                    ("measuring…" if s["running"] else "—"))

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Log")
        st.code("\n".join(list(R.logs)[-30:]) or "(nothing yet)", language=None, height=360)
    with right:
        st.subheader("Excel")
        if run and not s["running"] and run.get("output_path") and Path(run["output_path"]).exists():
            download_button(run["output_path"], "Download Excel", key=f"dl-{run['output_path']}")
        elif not s["running"]:
            last = store.last_output()
            if last and Path(last["output_path"]).exists():
                st.caption(f"From run #{last['id']}:")
                download_button(last["output_path"], "Download Excel", key=f"dl-{last['output_path']}")
        if run:
            if st.button("Export what I have now", icon=":material/table_view:",
                         help="Writes an Excel with everything collected so far (the run keeps going)."):
                with st.spinner("Writing Excel…"):
                    st.session_state["partial_export"] = str(R.export_now(run["id"]))
            partial = st.session_state.get("partial_export")
            if partial:
                download_button(partial, "Download partial Excel", key=f"dlp-{partial}")
        if run and s["errors"]:
            with st.expander(f"Recent errors ({s['errors']})"):
                for e in store.errors(run["id"], 20):
                    st.text(f"{e['ts']}  {e['context']}: {e['message'][:300]}")


progress_panel()

st.divider()
st.caption("A main phone starting with 076 is B144's call-tracking number; the business's own numbers are "
           "under Mobile phone / Other phones when listed. B144's terms restrict automated collection, and mobile numbers of sole "
           "proprietors are personal data under Israel's Privacy Protection Law. How you use the data is "
           "your responsibility.")
