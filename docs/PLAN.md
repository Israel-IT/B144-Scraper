# Original approved plan

> Copied verbatim from the planning session (source: `~/.claude/plans/i-want-to-biuld-clever-puffin.md`, 2026-09-23).
> Several findings in it turned out to be wrong or incomplete once implementation started. See
> [DECISIONS.md](DECISIONS.md) for what changed and why, and [SITE_FINDINGS.md](SITE_FINDINGS.md) for how the site actually behaves.

---

## B144 business directory scraper → Excel, with a local web UI

## Context
You want a local Python tool that goes through every business category on https://www.b144.co.il/, covers the whole country for each one, collects every available detail about each business (names, phones, mobiles, address, website, social links, hours, ratings and so on), and writes it all to one Excel file. A simple web page with a Run button starts the job and offers the Excel for download when it finishes.

Decisions already made: one combined sheet with filters plus a Summary sheet; a Streamlit web UI; uv to manage Python 3.12. The project is new, at `/Users/myroslavpap/Projects/b144-scraper/`, with no existing code to reuse.

## Scraping library: Scrapling (not crawl4ai)
While exploring the site I found that every piece of data we need is already structured JSON. It's embedded in each page's `__NEXT_DATA__` or returned by a JSON search API, and all of it comes back over **plain HTTP**. I checked this with curl and got 200 responses with full data, no browser needed.
- **Scrapling** `FetcherSession` is built on curl_cffi with browser TLS impersonation. It's fast and light, and it helps with the site's F5 bot-protection cookies (`TS…`). If the site ever starts challenging requests, `StealthyFetcher` is a built-in fallback. Scrapling's parser handles the HTML.
- **crawl4ai** is built for turning pages into LLM-friendly markdown with a Playwright browser. That's the wrong tool here: it would be about 10× slower across roughly 100k+ pages and adds nothing when the data is already JSON.

## What I found about the site
| Need | Source |
|---|---|
| Full category list (about 1,500) | `/indexes/` → `pageProps.CitiesListsTbl` (115 cities). Then each `/indexes/{city}/` → `pageProps.CategoriesTbl` (`catCode`, `catDesc`, `link`). Take the union by `catCode`. (The site's own A–Z list at `/indexes/categories/{letter}/` is broken and always returns the "א" list, so I don't use it.) |
| Whole-country coverage | Each category is split into 15 regions, `/{cat}/אזור-…/`, with region codes `-100`…`-114`. Their counts add up to the national total (electricians: 1,914 across regions vs 1,915 nationally; the one extra is a national "ארצי" listing on the category landing page `/{cat}/`, which I also collect). |
| Listing rows (15 per page) | `POST https://services.b144.co.il/Services/b144SearchService.asmx/getSearch` with form field `jsDetails` = JSON `{Category, Category_Code, City: region slug, CityCode: "-10x", PageIndex: 1.., isCardOpen: true, …}` and header `Authorization: Bearer <ApiToken cookie, URL-decoded>`. The `ApiToken` cookie is set by any normal page GET. The response is XML-wrapped JSON (`<string>…</string>` → `html.unescape` → `json.loads`) → `Response[]` with `TotalCount`. `PageIndex=1` is the same as the server-rendered first page. |
| Full business details | `GET /b144_sip/{MID}/` → `pageProps.initialMemberPageData`: `Phone` (076 virtual number), `MobilePhone` (the real mobile), `MorePhoneNumbers`, `Address`, `Street`, `Street_No`, `City`, `Zip_code`, `MapX/Y`, `MemberOpenHours`, `AreasServices`, `Categories`, `SubCategories`, `Additionals`, `memberLanguages`, `links`, `IsWhatsapp`, ratings, description. Listing rows add `Web`, `FAddress` (Facebook), `InstagramAddress`, and `Service_Location`. |

In listing rows, phones are masked (`...076-801`) except for the first few cards on each page, so fetching the detail page is required to get full phone numbers.

## Project layout
All code goes in a new project folder: **`/Users/myroslavpap/Projects/b144-scraper/`**.
```
b144-scraper/
  run.sh                # one-command launcher: (re)starts the app, see "Run script" below
  Run B144 Scraper.command  # double-clickable Finder wrapper that calls run.sh
  pyproject.toml        # uv: python 3.12, scrapling[fetchers], streamlit, xlsxwriter
  README.md             # setup + run instructions
  app.py                # Streamlit UI
  b144/
    __init__.py
    http.py             # FetcherSession wrapper: UA, retries/backoff, rate limit, ApiToken refresh, WAF-challenge fallback to StealthyFetcher
    parse.py            # extract __NEXT_DATA__ pageProps; unwrap the XML-wrapped API JSON
    categories.py       # build/cache the category list from the city indexes
    listings.py         # per category: landing + 15 regions → paginate getSearch
    details.py          # per unique MID: fetch b144_sip page → normalized dict
    store.py            # SQLite (data/b144.sqlite): categories, listings, details, progress; makes runs resumable
    export.py           # Excel writer
    runner.py           # orchestrates the pipeline in a background thread, reports progress, honors the stop flag
    __main__.py         # CLI: `uv run python -m b144 --category חשמלאים --no-details --out x.xlsx`
  data/                 # sqlite + output xlsx (gitignored)
```

## Pipeline (runner.py)
1. **Categories.** Load from SQLite. If the cache is empty or "Refresh categories" is clicked, rebuild from the 115 city index pages (about 116 requests).
2. **Listings.** For each selected category that isn't finished yet:
   - GET `/{slug}/` to get the national listing(s) and refresh the token.
   - For each of the 15 regions, POST getSearch for PageIndex 1…ceil(TotalCount/15), stopping early when a page comes back empty.
   - Upsert rows keyed on `(catCode, MID)` and record each finished region/page so an interrupted run resumes where it stopped.
   - The exact `Category` string the API expects for multi-word categories (dashes or spaces) will be confirmed against a real one such as `עורכי-דין` in step 1 of Verification. If the API rejects it, pages 2+ fall back to the server-rendered region page.
3. **Details** (on by default, toggle in UI). For every unique MID without a stored detail record, GET the detail page. Details are cached per MID, so a business listed in several categories is fetched once.
4. **Export** to `data/b144_businesses_YYYYMMDD_HHMM.xlsx`.

**Politeness and robustness.** About 2 concurrent requests with a random 0.3–0.8 s delay, both configurable. Retries with exponential backoff on 403/429/5xx. If an API response has `Success:false` or returns 401, the token is refreshed by re-GETting a page. If an HTML response has no `__NEXT_DATA__`, it's treated as a WAF challenge: the session is re-primed with `StealthyFetcher`, and if that fails the job pauses with a clear log message. Robots.txt doesn't disallow category pages or `/b144_sip/`.

**Runtime.** A full run is roughly 15–25k search calls plus about 100k detail pages, which is **many hours (likely overnight)**. That's why the job is resumable and the UI lets you pick a subset of categories. A 10-category test should take minutes.

## Excel format (export.py, xlsxwriter in constant_memory mode, sheets set right-to-left for Hebrew)
**Sheet "Businesses".** One row per (category, business), frozen header, auto-filter, set column widths. Columns:
Category · Category code · Business name · Main phone (B144 virtual 076) · Mobile phone · Other phones · WhatsApp (Y/N) · Website · Facebook · Instagram · Other links · Street · Street no. · City · Zip · Full address · Service location · Regions served · Subcategories · Additional services/tags · Languages · Opening hours · Rating avg · Rating count · Description · Latitude · Longitude · B144 page URL · Member ID · MID · Scraped at.

**Sheet "Summary".** Category, catCode, business count, # with mobile, # with website.

**Sheet "Run info".** Start and end time, categories covered, totals, errors.

## UI (app.py, Streamlit)
- Sidebar: category multiselect (default: all) with a search box, a "Fetch full details (phones/mobiles)" checkbox, concurrency and delay sliders, and a "Refresh category list" button.
- Main: **Run / Stop / Resume** buttons. A progress bar with counters (categories done/total, listings found, details fetched, errors, ETA) and a live log tail. The progress area refreshes every 2 s via `st.fragment(run_every=2)`.
- When the job finishes, a **Download Excel** button appears. There's also an "Export what I have now" button for partial results.
- The runner lives in a background thread held by `st.cache_resource`, so it keeps running when the page reruns and the browser tab can be reopened. State lives in SQLite.

## Setup (done during implementation)
1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (goes to `~/.local/bin`).
2. `cd b144-scraper && uv sync` (uv downloads Python 3.12 and the dependencies).
3. Run: `uv run streamlit run app.py` → opens http://localhost:8501.

## Run script (`run.sh`), delivered at the end
Running `./run.sh` from anywhere (or double-clicking `Run B144 Scraper.command`) does the following:
1. `cd`s into the project folder.
2. Installs uv if it's missing and runs `uv sync`, which is quick when nothing has changed. The first run on a new machine therefore works too.
3. **Restarts if already running:** reads `data/app.pid` and, if that process is alive, stops it (TERM, then KILL after 5 s). It also frees port 8501 if something else from this app is still holding it (`lsof -ti :8501`).
4. Starts `uv run streamlit run app.py --server.port 8501 --server.headless true` in the background with `nohup`, writes the new PID to `data/app.pid`, logs to `data/app.log`, waits until the port answers, then opens http://localhost:8501 in your browser.
5. Also supports `./run.sh stop` and `./run.sh status`.

Restarting stops any scrape in progress, but no data is lost. Progress is in SQLite, and after the restart the UI shows "Unfinished run found — Resume".

**Docker isn't needed.** uv keeps Python and the dependencies isolated inside the project, and the app uses plain HTTP with no browser to install. Skipping Docker keeps startup instant. I'll add an optional `Dockerfile` + `docker-compose.yml` (with `restart` semantics via `docker compose up -d --force-recreate`) only if you ask.

## Verification
1. CLI smoke test on a small category with `--no-details`: the row count should match the site's `TotalCountAllResults` (±1 for the national listing). Then the same for a multi-word category (`עורכי-דין`) to confirm the API's Category format.
2. Run details for about 20 MIDs and check that mobile, other phones, zip and hours are filled in and match what the browser shows on those pages.
3. Open the Excel: Hebrew displays right-to-left, filters work, and the Summary counts match.
4. UI: start the app, pick 3 categories, Run, Stop midway, Resume, confirm there are no duplicate rows, then Download. I'll drive it through the browser pane.
5. Rebuild the category list and confirm the unique count is about 1,500.
6. Run script: `./run.sh` starts the app. Running `./run.sh` again while it's running stops the old PID and starts a new one (checked with `./run.sh status` and the PID changing). `./run.sh stop` frees port 8501.

## Caveats to keep in mind
- The "Main phone" column holds B144's 076 call-tracking number. The real number is in "Mobile phone" or "Other phones" when the business has one listed.
- B144's Terms of Service restrict automated collection. Mobile numbers of sole proprietors count as personal data under Israel's Privacy Protection Law (Amendment 13). How the data is used is your responsibility; the tool keeps request rates modest.
