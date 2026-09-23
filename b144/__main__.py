"""CLI.

  uv run python -m b144 --category חשמלאים --no-details --out data/electricians.xlsx
  uv run python -m b144 --category עורכי-דין --category 4022 --details-limit 20
  uv run python -m b144 --refresh-categories
  uv run python -m b144 --resume
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from . import DB_PATH
from .export import export_run
from .runner import DEFAULT_CONCURRENCY, MAX_CONCURRENCY, Runner

log = logging.getLogger("b144")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="b144", description="Scrape b144.co.il into Excel")
    ap.add_argument("--category", "-c", action="append", default=[],
                    help="category name, slug or catCode (repeatable). Omit for all categories.")
    ap.add_argument("--no-details", action="store_true", help="skip the per-business detail pages")
    ap.add_argument("--no-city-sweep", action="store_true",
                    help="skip the city-page sweep that catches businesses missing from the regional lists")
    ap.add_argument("--details-limit", type=int, help="fetch details for at most N businesses (testing)")
    ap.add_argument("--out", type=Path, help="output .xlsx path (default data/b144_businesses_<date>.xlsx)")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"parallel requests, 1–{MAX_CONCURRENCY} (default {DEFAULT_CONCURRENCY})")
    ap.add_argument("--delay", type=float, nargs=2, default=(0.3, 0.8), metavar=("MIN", "MAX"))
    ap.add_argument("--refresh-categories", action="store_true", help="rebuild the category list and exit")
    ap.add_argument("--resume", action="store_true", help="resume the last unfinished run")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args(argv)
    args.concurrency = max(1, min(MAX_CONCURRENCY, args.concurrency))
    for stream in (sys.stdout, sys.stderr):  # Hebrew output on a Windows console (cp1252 by default)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    log.addHandler(console)

    runner = Runner(args.db)
    if args.refresh_categories:
        runner.refresh_categories(args.concurrency, tuple(args.delay))
        _wait(runner)
        print(f"{len(runner.store.categories())} categories in the list")
        return 0

    if args.resume:
        run_id = runner.resume()
    else:
        codes = None
        if args.category:
            if not runner.store.categories():
                runner.refresh_categories(args.concurrency, tuple(args.delay))
                _wait(runner)
            codes = []
            for term in args.category:
                matches = runner.store.find_categories(term)
                if not matches:
                    print(f"Unknown category: {term}", file=sys.stderr)
                    return 2
                codes += [m["cat_code"] for m in matches]
        run_id = runner.start(codes, fetch_details=not args.no_details, concurrency=args.concurrency,
                              delay=tuple(args.delay), details_limit=args.details_limit,
                              city_sweep=not args.no_city_sweep)
    _wait(runner)

    run = runner.store.run(run_id)
    print(f"\nRun {run_id}: {run['status']} {run.get('note') or ''}")
    print(f"{'category':<28}{'site total':>11}{'Σ regions':>11}{'rows':>8}{'via cities':>11}  status")
    for p in runner.store.category_report(run_id):
        print(f"{p['name'][:27]:<28}{p['site_total'] or 0:>11}{p['region_total'] or 0:>11}{p['rows'] or 0:>8}"
              f"{p['city_rows'] or 0:>11}  {p['status']} {p['error'] or ''}")
    c = runner.store.counters(run_id)
    print(f"rows={c['listings']} unique businesses={c['businesses']} with details={c['details_done']} "
          f"errors={c['errors']}")
    if args.out:
        path = export_run(runner.store, run_id, args.out)
        print(f"Excel: {path}")
    elif run.get("output_path"):
        print(f"Excel: {run['output_path']}")
    return 0 if run["status"] == "finished" else 1


def _wait(runner: Runner):
    try:
        while runner.is_running():
            time.sleep(0.5)
    except KeyboardInterrupt:
        runner.stop()
        runner.wait()


if __name__ == "__main__":
    sys.exit(main())
