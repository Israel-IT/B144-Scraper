# B144 scraper

Scrapes every business category on [b144.co.il](https://www.b144.co.il/) across the whole country
and writes one Excel file. A small local web page (Streamlit) has Run / Stop / Resume buttons,
live progress and a Download Excel button.

## Start it

**Mac:** `./run.sh`, or double-click **`Run B144 Scraper.command`** in Finder.

**Windows:** double-click **`run.bat`**, or run `run.bat` in a Command Prompt / PowerShell in this folder.
Copy the whole folder to the PC. A `.venv` copied over from the Mac is detected and rebuilt. Nothing else needs
to be installed beforehand.

Either way it:

1. installs [uv](https://docs.astral.sh/uv/) if it's missing, then runs `uv sync` (uv downloads Python 3.12 and the dependencies; only the first run takes a few minutes);
2. stops the app if it's already running (restart), freeing port 8501;
3. starts it in the background and opens <http://localhost:8501>.

```bash
./run.sh status   # running? which PID?   (Windows: run.bat status)
./run.sh stop     # stop the app and free port 8501   (Windows: run.bat stop)
```

Logs: `data/app.log` (server; on Windows also `data/app.err.log`) and `data/scrape.log` (scraper). The app keeps running after you
close the terminal or the browser tab; reopen <http://localhost:8501> any time.

## Using the page

* **Sidebar**: leave *All categories* on, or switch it off and pick categories (type to search).
  *Concurrent requests* (1–100, default 20) is the speed control. Listings and business details share
  these requests, and several categories run at once. The random delay per request is 0.3–0.8 s by default.
  Every run fetches each business's page (mobile, other phones, zip, hours, languages) and runs the city
  sweep ([Coverage](#coverage)). The UI has no switches for these; the CLI still does.
* **Run** starts a fresh run. **Stop** finishes the in-flight requests and pauses.
  **Resume** continues an unfinished run from the next page. That includes a run that was cut off by a
  restart or a crash: the page then shows "Unfinished run found — Resume".
* **Download Excel** appears when a run finishes. **Export what I have now** writes a partial file
  at any time.

A full run (≈1,430 categories plus details for every unique business) takes several hours even at high
concurrency, so plan it overnight. Everything is saved to `data/b144.sqlite` page by page, so nothing is lost on Stop or restart.
Business details are cached per business, so a business listed in several categories is fetched once,
and a later run reuses them.

## Excel layout

The file name says what the run covered: `b144_all-categories_<date>.xlsx`, `b144_<category>_<date>.xlsx` or
`b144_<n>-categories_<date>.xlsx`. The page shows the same label above the Download button.

* **Businesses**: one row per (category, business); frozen header, filters, right-to-left.
  Category · Category code · Business name · Main phone · Mobile phone · Other phones ·
  Email · Fax · WhatsApp · Website · Facebook · Instagram · Other links · Street · Street no. · City · Zip ·
  Full address · Service location · Regions served · Subcategories · Additional services/tags · Languages ·
  Opening hours · Rating avg · Rating count · Description · Latitude · Longitude · B144 page URL ·
  Member ID · MID · Scraped at · Full details (Y/N).
* **Summary**: per category: businesses, # with mobile, # with website, count listed on the site,
  Σ of the regional counts, # found only via the city sweep, status.
* **Run info**: start/end, totals, error log.

Phones come from each business's own page (the *Fetch full details* step). The category lists B144 shows
mask them ("...09-7434"), so rows with Full details = N have almost no phones. A main phone starting with
076 is B144's call-tracking number, which forwards to the business. The business's own numbers are then
under Mobile phone / Other phones when it lists any. Mobile phone shows B144's mobile field, or else the first 05x
number the business has.

Listings and details happen in the same pass. As soon as a page of listings is saved, its businesses' pages
are queued, and a category counts as finished only once its details are in. So every finished category is
complete in the Excel, even in a partial export.

## Command line

```bash
uv run python -m b144 --category חשמלאים --no-details --out data/electricians.xlsx
uv run python -m b144 -c עורכי-דין -c 4022 --details-limit 20   # name, slug or catCode
uv run python -m b144 -c עורכי-דין --no-city-sweep            # regions only
uv run python -m b144 --refresh-categories
uv run python -m b144 --resume
uv run python -m b144 -c חשמלאים --concurrency 50            # default 20, max 100
```

Don't run the CLI while the web app is scraping. Both share `data/b144.sqlite`, and starting one marks
the other's in-progress run as interrupted.

## How it works

Everything is plain HTTP through Scrapling's `FetcherSession` (curl_cffi with a Chrome TLS fingerprint).
No browser is needed.

| Step | Source |
|---|---|
| Categories | `/indexes/{letter}/` for each Hebrew letter + `/indexes/cities/` → `/indexes/{city}/`, unioned by catCode |
| Whole country | category landing `/{slug}/` → national ("ארצי") listings + links to the up-to-15 regions `/{slug}/אזור-…/` (region codes −100…−114, cached) |
| Listings | `POST services.b144.co.il/…/getSearch` with the page's own `seoAnalyzerObj` + `PageIndex`, `Authorization: Bearer <ApiToken cookie>`; 15 rows per page |
| City sweep | for regions whose count on the landing page is higher than their region list: each city page `/{slug}/{city}/` of that region (the region's city list + index cities), top "serves this city" group only |
| Details | `/b144_sip/{MID}/` → `initialMemberPageData` |

With many requests in flight the site sometimes answers slowly (a few 30 s timeouts), and those requests are
retried. On HTTP 403 or 429, every worker pauses for the backoff time, not only the one that got it.
If the site starts serving bot challenges (HTML with no `__NEXT_DATA__`), the scraper re-primes the
session. If that fails it tries Scrapling's `StealthyFetcher`, which needs a browser: `uv run scrapling install`.
If it still can't get through, it pauses the run with a message, and you can Resume later.

Code map: `b144/http.py` (session, retries, token), `parse.py`, `categories.py`, `listings.py`,
`details.py`, `store.py` (SQLite), `export.py` (Excel), `runner.py` (background pipeline), `app.py` (UI).

## Coverage

The 15 regional lists cover almost every business, but not all of them. A business that lists individual
cities as its service area, rather than whole regions, is left out of the regional lists. It still appears
at the top of those cities' pages (the "serves this city" group, above the businesses located in the city).
The city sweep reads that group for every city in each region where the site's area count is higher than
what the region list returned.

Don't expect "Listed on site" to equal the row count, even with the sweep:

* A business serving several regions is counted in each region but kept once per category.
* Most of the remaining difference between the site's totals and the regional counts is how the site counts,
  not missing businesses. Fully paginating every city list in four regions for עורכי-דין found only the
  businesses the sweep also finds.

The sweep costs one page per city in the gap regions, a few hundred requests for a large category, and
often finds nothing. Switch it off (UI checkbox, or `--no-city-sweep`) for a faster run.

## Documentation

* [docs/DECISIONS.md](docs/DECISIONS.md): every design decision and why, what changed from the plan,
  runtime estimates, and open questions
* [docs/SITE_FINDINGS.md](docs/SITE_FINDINGS.md): how b144.co.il and its search API work (URLs, payloads, fields)
* [docs/VERIFICATION.md](docs/VERIFICATION.md): what was tested and the results
* [docs/PLAN.md](docs/PLAN.md): the original approved plan

## Caveats

B144's Terms of Service restrict automated collection. Mobile numbers of sole proprietors count as
personal data under Israel's Privacy Protection Law (Amendment 13). How you use the data is your
responsibility. The tool keeps request rates modest.
