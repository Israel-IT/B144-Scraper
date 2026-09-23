# Decision log

The decisions behind this project: what was chosen, why, and what changed from the
[original plan](PLAN.md). The newest decisions come last. Dates are 2026-09-23 unless noted.

## Decided before implementation (the approved plan)

| # | Decision | Why |
|---|---|---|
| D1 | **Scrapling** (`FetcherSession` over curl_cffi, plain HTTP), not crawl4ai | All the data is already JSON (`__NEXT_DATA__` plus a search API). A browser-based crawler would be ~10× slower and add nothing. curl_cffi's Chrome TLS fingerprint keeps the F5 WAF quiet. `StealthyFetcher` is kept only as a fallback. |
| D2 | **Python 3.12 via uv**. Dependencies: `scrapling[fetchers]`, `streamlit`, `xlsxwriter` | Isolated and reproducible, with no system Python needed. `uv sync` is instant when nothing changed. |
| D3 | **One Excel file**: sheet *Businesses* (one row per category × business; frozen header, auto-filter, right-to-left) + *Summary* + *Run info* | This was the requested format. Filters on a single sheet are easier to work with than one sheet per category. |
| D4 | **Streamlit UI** at `localhost:8501`. The runner is a background thread held by `st.cache_resource` | The run survives page reruns and closed tabs. |
| D5 | **State in SQLite** (`data/b144.sqlite`), saved page by page | Runs can be stopped and resumed, and a restart or crash loses nothing. |
| D6 | **No Docker** | uv already isolates the environment, and no browser is needed. |
| D7 | **Politeness**: 2 concurrent requests, random 0.3–0.8 s delay, exponential backoff on 403/429/5xx, token refresh on `Success:false` | Keeps the load on the site modest. |
| D8 | **`run.sh` launcher** with restart semantics (TERM, then KILL after 5 s, then free port 8501), `stop` / `status`, plus a double-clickable `.command` | One command to (re)start. |

## Changed or added during implementation

| # | Decision | Why |
|---|---|---|
| D9 | **Installed uv** at `~/.local/bin` | The brief said uv was already installed, but it wasn't. `run.sh` now installs it if it's missing. |
| D10 | **Category list = `/indexes/{letter}/` (22 Hebrew letters) ∪ `/indexes/cities/` → `/indexes/{city}/`**, unioned by `catCode`. The result was 1,430 categories. | The plan said the city list is at `/indexes/`. It isn't: that page is the "א" letter list. The city list is at `/indexes/cities/` (115 cities). The per-letter pages work fine. Only `/indexes/categories/{letter}/` is broken. The letter pages alone give 1,427 of the 1,430. |
| D11 | **Search API payload = the page's own `seoAnalyzerObj`** + `PageIndex` | That object carries the exact `Category` string the API expects. For multi-word categories it uses spaces (`"עורכי דין"`), not the dashes in the URL. With it, no server-rendered fallback is needed. |
| D12 | **National rows = `AreaName == "ארצי"`** on the landing page | Verified on several categories. |
| D13 | **Region codes (−100…−114) are cached per region slug**, so later categories skip the region-page GET | Fewer requests. The code depends only on the region, not the category. |
| D14 | **One persistent worker pool per run** (sessions are per worker thread) | Found in review: a new pool per category would have opened and leaked about 2 curl sessions and 2 token primes per category (~2,900 sessions in a full run). |
| D15 | **Per-item errors never kill the run.** A failed region or detail is logged to `errors`, and the category is marked `error` so Resume retries it. Only Stop and WAF-blocked end the run. | A long unattended run needs to survive one-off failures. |
| D16 | **Extra Excel columns**: Email, Fax, Full details (Y/N). Summary adds "Σ region counts" and "Found via city sweep". | Email and fax are on the detail page and cost nothing extra. The other columns make coverage and completeness visible. |
| D17 | **Hours as text**, one entry per day (`יום ראשון 09:00-18:00; …`), taken as the site shows them. Phones are deduplicated by digits. | The first version split day and time into separate items. The same mobile appeared twice in different formats. |

## Coverage

| # | Decision | Why |
|---|---|---|
| D18 | **Regional lists are the backbone. Their counts are *not* expected to equal the site's total.** | For electricians they match exactly (1,915 = 1,914 regional + 1 national). For lawyers the site says 11,690 but the regions sum to 11,583. Every region returned exactly its own `TotalCount`, and pagination is stable with no duplicates. |
| D19 | **City sweep** (`listings.sweep_cities`), added at the user's request | Some businesses list individual cities as their service area rather than regions, and the regional lists leave them out. They appear in the top "serves this city" group of those cities' pages, above the businesses located in the city (`isbyCity=1`). The sweep reads that group for every city of each region where the landing page's area count is higher than the region list's count. It uses page 1 per city, plus more pages only while the group continues. It resumes per city (`city_progress`), and each city's code is cached. |
| D20 | **Most of the count gap is how the site counts, not missing businesses** | Fully paginating every city list in 4 regions for lawyers found only the same businesses the sweep finds (5 of the 107 gap). For embassies, the sweep covered 144 cities and found 0 new, and Tel Aviv's full city list (129) was already fully collected. |
| D21 | **City sweep is always on in the UI** (decided later; see D30). `--no-city-sweep` still exists on the CLI. | Cost: roughly 150 extra page requests per category (lawyers ~370, embassies ~150), for well under 1% more businesses. At the new concurrency (D28) that costs hours, not days. |

## Phones, details and progress (after user feedback)

| # | Decision | Why |
|---|---|---|
| D22 | *(Superseded by D27.)* **Details are fetched right after each category's listings**, not after all categories. A resumed run first catches up on businesses already listed. | User report: "only few phone numbers in the Excel." B144's category lists mask phones (`...09-7434`), so phones come only from detail pages. With details at the end, any export during the first ~7 hours of a full run would have had almost no phones. Now every finished category is complete in the Excel. |
| D23 | **Column renamed "Main phone"** (it was "Main phone (B144 virtual 076)"). **Mobile phone** = B144's mobile field, or else the first 05x number the business has. | Many main phones are the business's own mobile (free listings), not B144's 076 call-tracking number. The old header was wrong, and the Mobile column looked empty. |
| D24 | **The UI shows the running run's own scope and settings** (categories, details on/off, city sweep on/off). The sidebar says its settings apply to the next run. | User report: "0/1 categories while All categories is selected." Run #7 was a 1-category CLI run, and the sidebar looked like it described it. |
| D25 | **The ETA and progress bar measure total work in requests**: each listing counts 1/15 of a request (15 per page) and each detail page counts 1. Expected listings come from each category's site total (saved as soon as its landing page is read), with an average of ~300 per category for categories not reached yet. | User report: "ETA shows —". The old ETA counted finished categories, so a one-category run had nothing to measure. |
| D26 | **A listings-only run shows a warning in the UI**, and rows without details are flagged `Full details = N` | A listings-only Excel is expected to have almost no phones. That must be obvious, not look like a bug. |

## Speed, one-pass pipeline and Windows (user request)

| # | Decision | Why |
|---|---|---|
| D27 | **Listings and details in one pass** (`details.DetailQueue`). Every saved page of listings (regional, national, city sweep) queues its businesses' detail pages on the shared worker pool right away. A category thread waits for its own details before it takes the next category. A resumed run queues its backlog at the start. A final catch-up retries failed detail pages once. This replaces D22's "listings, then details" per category. | User: "I don't want this separation in get companies and then add details, it should be in the same run." |
| D28 | **Concurrency is 1–100, default 20** (UI slider and `--concurrency`). **Several categories run at once**: `max(1, min(20, concurrency // 5))` category threads, each waiting on its region, city and detail requests in the one pool. The category threads' own landing-page requests come on top of the pool size. | User: "It's very slow, can we increase the limit of concurrency to 100." One category has at most 15 regions, so a big pool needs several categories to stay busy. |
| D29 | **A 403/429 pauses every worker** for the backoff time (`Client._pause_all`), not just the one that got it | With 100 workers, per-thread backoff alone would keep hitting the site while it is asking us to slow down. |
| D30 | **"Fetch full details" and "City sweep" removed from the UI**. Every UI run has both on. The CLI keeps `--no-details` / `--no-city-sweep` for testing. The run caption no longer shows them, and the listings-only warning is gone. | User: "fetch full details and city sweep is enabled by default and the user doesn't need to know about it." |
| D31 | **Windows launcher**: `run.bat` (double-click, or `run.bat stop` / `status`) calls `run.ps1` with `-ExecutionPolicy Bypass`. It installs uv, replaces a `.venv` copied from a Mac, runs `uv sync`, restarts the app on port 8501 hidden in the background (`--server.address localhost`, so no firewall prompt), waits for `/_stcore/health` and opens the browser. It stops the app by killing the process tree (`taskkill /T`). The venv's `python.exe` starts a child process. | User: "I want to be able to run this project in my Windows PC with one command." `.bat` gets around PowerShell's default script policy. The scripts are ASCII with CRLF line endings, so Windows PowerShell 5.1 reads them correctly. |

## Runtime estimate (for planning a full run)

Measured with the one-pass pipeline (D27):

| Concurrency | Throughput | Notes |
|---|---|---|
| 2 (old default) | ~1 request/s | The old estimate was 3–6 days for a full run. |
| 20 | ~10 requests/s | 3 categories, 1,867 businesses with details, 3 min 22 s |
| 40 | ~16–18 requests/s | The site starts answering slowly (a few 30 s timeouts, retried) |
| 100 | not measured | Expect more timeouts. Throughput is likely capped by the site's response time, not the pool. |

A full run is about 480k requests: ~29k listing pages, ~250k detail pages (a guess) and ~200k city-sweep pages.
That's ~13 h at 10 req/s and ~8 h at 16 req/s.

## Not verified yet

* The `StealthyFetcher` fallback: the site never challenged us, and its browser isn't installed (`uv run scrapling install`).
* Double-clicking `Run B144 Scraper.command` in Finder (only its syntax was checked).
* A full all-categories run.
* `run.bat` / `run.ps1` on a real Windows PC. They were only parse-checked with PowerShell 7 on the Mac.
* Concurrency 100 against the live site.
* The city sweep end to end on lawyers: the prototype found 5 extra businesses, but the integrated run (#7) is still in progress.
