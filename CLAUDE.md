# b144-scraper: notes for future sessions

Read these before changing anything:
- [docs/DECISIONS.md](docs/DECISIONS.md): what was decided and why, including the city sweep default
  (D21: decided, always on).
- [docs/SITE_FINDINGS.md](docs/SITE_FINDINGS.md): how the site and API behave. Verify against the live site
  before relying on it.
- [docs/VERIFICATION.md](docs/VERIFICATION.md): what has been tested and what hasn't.
- [docs/PLAN.md](docs/PLAN.md): the original approved plan (partly superseded).

Working rules:
- Run and test with `.venv/bin/python -m b144 …`; `--db <copy>.sqlite` tests without touching the UI's run
  (the log still goes to `data/scrape.log`). Start or restart the UI with `./run.sh` (`stop`, `status`).
  Windows: `run.bat` → `run.ps1` (parse-checked only, never run on Windows).
- **Don't run the CLI while the UI is scraping.** Both use `data/b144.sqlite`, and creating a `Runner` marks any
  `running` run as `interrupted`.
- The UI process keeps the imported code until it restarts: `./run.sh`, then Resume in the page.
- `data/` is gitignored: SQLite state, Excel outputs, logs. Business details are cached per MID across runs.
- Default is 20 concurrent requests (max 100) with a 0.3–0.8 s delay, and a 403/429 pauses all workers (D29).
  Watch `data/scrape.log` for WAF signs (pages without `__NEXT_DATA__`).
- Record new decisions in `docs/DECISIONS.md` and new test results in `docs/VERIFICATION.md`.
