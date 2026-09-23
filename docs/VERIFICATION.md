# Verification log

Checks run on 2026-09-23 against the live site. Run numbers refer to `runs.id` in `data/b144.sqlite`.

## 1. Category list
`python -m b144 --refresh-categories` read 115 cities + 22 letters in 1 min 23 s, giving **1,430 unique categories**
(1,427 from the letter lists alone). The plan expected about 1,500. ✅

## 2. Listing counts (CLI, `--no-details`)

| Run | Category | Site total | Σ regions (+ national) | Fetched | Unique rows | Result |
|---|---|---|---|---|---|---|
| 1 | באולינג | 40 | 40 (9 regions) | 40 | 39 | ✅ exact. One business is in two regions |
| 2 | חשמלאים | 1,915 | 1,914 + 1 = 1,915 | 1,915 | 1,755 | ✅ exact |
| 2 | עורכי דין | 11,690 | 11,583 | 11,583 | 11,058 | ⚠️ each region is complete, but −107 against the site total (see [SITE_FINDINGS.md](SITE_FINDINGS.md#city-pages-slugcity-and-the-coverage-gap)) |

The multi-word category worked through the API using the `Category` value from `seoAnalyzerObj` (with spaces).
Timing: electricians took 1.5 min and lawyers 11 min.

## 3. Details
* 20 older free listings: all 20 had a main phone. No mobiles or hours, because those listings don't have them.
* 20 paid listings from the Tel Aviv region: main phone 20/20, mobile 3, other phones 2–3, zip 12, hours 6, email 4.
* Browser check of MID `4A1404134470655D49170110477164584A` (טאגור סנטר). Main 03-5331153, mobile 0507658563,
  other 0547741325, zip 6920340, all 7 days of hours and languages all match the live page. ✅
* Found and fixed during this check: the hours day/time split and the duplicate mobile in Other phones.

## 4. Excel
Checked by reading the XML and by opening the file in openpyxl:
* Sheets are Businesses, Summary and Run info. Businesses and Summary are right-to-left with a frozen header and
  an auto-filter over all rows and columns.
* Hebrew text is intact and there are no duplicate (category, MID) rows.
* The Summary counts match. ✅

## 5. UI (driven in the Claude browser pane)
* Picked 3 categories (באולינג, שגרירות, אבחון דידקטי) and pressed Run (run 4), then Stop mid-category. It stopped
  on שגרירות / Jerusalem at page 3 of 4, and the page showed "Unfinished run found — Resume".
* Resume finished the run. The interrupted region ended at 53 of 53, and the run had 360 rows with 360 distinct
  (category, MID) pairs. ✅
* Download Excel delivered the exact file (HTTP 200, 73,651 bytes, same as on disk). ✅
* Restart during a scrape (run 5): after `./run.sh`, the page showed run 5 as "interrupted" with Resume. It
  resumed and finished with the same 360 rows and 0 errors. ✅
* Found and fixed: the national row was re-counted on resume (counter only), and the Run/Stop/Resume labels
  were truncated in narrow windows.

## 6. run.sh
* `./run.sh` starts the app. Running it again stops the old PID and starts a new one (76543 → 76604).
* `status` reports running or stopped. `stop` removes the PID file and frees port 8501. ✅
* The port listener is streamlit's Python child process. `run.sh` signals both uv and the child.

## 7. City sweep (added later)
* Run 6 (3 small categories): 6 gap regions for שגרירות, 144 cities swept, 0 new. There was nothing to sweep
  for the other two categories. ✅ Correct, but costly for no gain.
* Prototype on עורכי דין (same logic): 14 gap regions, 372 requests, 8 min, **5 businesses** missing from every
  regional list.
* Full pagination of every city list in 4 regions found no businesses beyond those the sweep finds, so reading
  only the top "serves this city" group is enough.
* Integrated run on עורכי דין (run 7): **in progress** at the time of writing.

## 8. Phones and progress fixes (after user feedback)
* Run 7 was switched from listings-only to details on (`UPDATE runs SET fetch_details=1 WHERE id=7`). It
  resumed and fetched details for the lawyers it had already listed. The first 94 all have a main phone and 30
  have extra numbers.
* The UI shows the run's own scope ("Run #7 · 1 category: עורכי דין · full details: on · city sweep: on"), the
  estimated time left (about 3 h for 11k details) and a progress bar over listings + details.

## 9. One-pass pipeline, concurrency and Windows launcher
Tested with the CLI on a copy of the database (`--db scratchpad/test.sqlite`), so UI run 7 wasn't touched:
* Run 8: חשמלאים + באולינג + אבחון דידקטי at concurrency 20, with details and without the sweep. It took 3 min 22 s:
  1,867 rows, 1,867 of 1,867 with details, 0 errors. The three categories ran in parallel. ✅
* Run 9: אינסטלטורים + שיפוצים at concurrency 40. **Stop** after 40 s (2,402 detail pages queued) took 4 s,
  and the status was `stopped`. **Resume** finished in 158 s with 2,996 of 2,996 businesses with details and 0 errors. ✅
* Load on the site across both runs (~5,500 requests): 5 timeouts (30 s) and 1 HTTP 500, all recovered by retry.
  No 403/429, no WAF challenges.
* `run.ps1` passes the PowerShell 7 parser (`Parser.ParseFile`, no errors). It has **not been run on Windows**.

## Not verified
* The StealthyFetcher fallback: the site never served a challenge, and the browser isn't installed.
* Double-clicking `Run B144 Scraper.command` in Finder (only its syntax was checked).
* `run.bat` / `run.ps1` on Windows.
* Concurrency 100.
* A full all-categories run (estimated at 3–6 days; see [DECISIONS.md](DECISIONS.md#runtime-estimate-for-planning-a-full-run)).
