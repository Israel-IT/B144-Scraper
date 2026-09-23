"""Background pipeline: categories -> listings -> details -> Excel, with Stop / Resume."""

import logging
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import DATA_DIR, DB_PATH
from .categories import build_categories
from .details import DetailQueue
from .export import export_run
from .http import Client, StopRequested, WafBlocked
from .listings import scrape_category
from .store import Store, now

log = logging.getLogger("b144")

DEFAULT_CONCURRENCY = 20
MAX_CONCURRENCY = 100


def category_parallelism(concurrency: int) -> int:
    """Categories worked on at once: enough to keep `concurrency` requests busy (a category has at most
    15 regions to page through), but not so many that each one takes ages to finish."""
    return max(1, min(20, concurrency // 5))


class _BufferHandler(logging.Handler):
    def __init__(self, buffer: deque):
        super().__init__()
        self.buffer = buffer

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass


def setup_logging(buffer: deque | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) for h in log.handlers):
        fh = logging.FileHandler(DATA_DIR / "scrape.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(message)s"))
        log.addHandler(fh)
    if buffer is not None:
        bh = _BufferHandler(buffer)
        bh.setFormatter(fmt)
        log.addHandler(bh)


class Runner:
    def __init__(self, db_path: Path = DB_PATH):
        self.logs: deque[str] = deque(maxlen=400)
        setup_logging(self.logs)
        self.store = Store(db_path)
        self.store.mark_interrupted()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict = {"phase": "idle", "run_id": None, "message": ""}
        self._phase_clock: dict = {}
        self._active: dict[str, str] = {}  # categories in progress: cat_code -> name
        self._active_lock = threading.Lock()
        self._details: DetailQueue | None = None

    # ---- control ------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, cat_codes: list[str] | None, fetch_details: bool = True,
              concurrency: int = DEFAULT_CONCURRENCY, delay: tuple[float, float] = (0.3, 0.8), details_limit: int | None = None,
              city_sweep: bool = True) -> int:
        if self.is_running():
            raise RuntimeError("A run is already in progress")
        run_id = self.store.create_run(cat_codes, fetch_details, concurrency, delay, city_sweep)
        log.info("Run %d started: %s categories, details=%s, city sweep=%s, concurrency=%d, delay=%.1f–%.1fs",
                 run_id, "all" if cat_codes is None else len(cat_codes), fetch_details, city_sweep, concurrency,
                 *delay)
        self._launch(self._work, run_id, details_limit)
        return run_id

    def resume(self, run_id: int | None = None) -> int:
        if self.is_running():
            raise RuntimeError("A run is already in progress")
        run = self.store.run(run_id) if run_id else self.store.unfinished_run()
        if not run:
            raise RuntimeError("No unfinished run to resume")
        self.store.set_run(run["id"], status="running", note=None)
        log.info("Resuming run %d", run["id"])
        self._launch(self._work, run["id"], None)
        return run["id"]

    def refresh_categories(self, concurrency: int = DEFAULT_CONCURRENCY, delay=(0.3, 0.8)):
        if self.is_running():
            raise RuntimeError("A run is already in progress")
        self._launch(self._refresh_work, concurrency, delay)

    def stop(self):
        if self.is_running():
            log.info("Stop requested — finishing in-flight requests…")
            self._stop.set()

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _launch(self, target, *args):
        self._stop.clear()
        self._thread = threading.Thread(target=target, args=args, name="b144-runner", daemon=True)
        self._thread.start()

    # ---- status for the UI ---------------------------------------------------------------
    def _set_phase(self, phase: str, units_done: int = 0, message: str = ""):
        self.state.update(phase=phase, message=message)
        self._phase_clock = {"phase": phase, "t0": time.time(), "done0": units_done}

    def _eta(self, done: int, total: int) -> float | None:
        clock = self._phase_clock
        elapsed = time.time() - clock.get("t0", time.time())
        progressed = done - clock.get("done0", 0)
        if progressed <= 0 or elapsed < 5 or total <= done:
            return None
        return (total - done) * elapsed / progressed

    def _work_units(self, run: dict) -> tuple[float, float]:
        """(done, expected) work in requests: 15 listings share one page request, each detail page is one."""
        lp = self.store.listing_progress(run["id"])
        done, expected = lp["done"] / 15, lp["expected"] / 15
        if run.get("fetch_details"):
            c = self.store.counters(run["id"])
            per_listing = c["businesses"] / lp["done"] if lp["done"] else 0.9  # unique businesses per listing
            done += c["details_done"]
            expected += max(c["businesses"], per_listing * lp["expected"])
        return done, expected

    def status(self) -> dict:
        running = self.is_running()
        run_id = self.state.get("run_id")
        run = self.store.run(run_id) if run_id else self.store.latest_run()
        out = {"running": running, "phase": self.state["phase"] if running else "idle",
               "message": self.state.get("message", ""), "run": run, "eta": None}
        if run:
            c = self.store.counters(run["id"])
            out.update(c)
            out["scope"] = self.store.run_scope(run["id"])
            lp = self.store.listing_progress(run["id"])
            out["listings_done"], out["listings_expected"] = lp["done"], lp["expected"]
            phase = self.state["phase"]
            if running and phase == "scraping":
                out["work_done"], out["work_expected"] = self._work_units(run)
                out["eta"] = self._eta(out["work_done"], out["work_expected"])
                out["message"] = self._scraping_message()
            elif running and phase == "details":
                out["eta"] = self._eta(c["details_done"], c["businesses"])
        out["categories_known"] = len(self.store.categories())
        return out

    def _scraping_message(self) -> str:
        with self._active_lock:
            names = list(self._active.values())
        parts = []
        if names:
            shown = ", ".join(names[:4]) + (f" +{len(names) - 4} more" if len(names) > 4 else "")
            parts.append(f"Working on {len(names)} categor{'y' if len(names) == 1 else 'ies'}: {shown}")
        details = self._details
        if details is not None and details.pending():
            parts.append(f"{details.pending():,} detail pages queued")
        return " · ".join(parts)

    # ---- workers ------------------------------------------------------------------------------
    def _refresh_work(self, concurrency, delay):
        client = Client(self._stop, delay=delay)
        pool = ThreadPoolExecutor(max(1, concurrency), thread_name_prefix="b144-worker")
        try:
            self._ensure_categories(client, pool, force=True)
        except StopRequested:
            log.info("Category refresh stopped")
        except Exception as e:
            log.error("Category refresh failed: %s", e)
        finally:
            self._stop.set()  # release any worker still sleeping
            pool.shutdown(wait=True, cancel_futures=True)
            client.close()
            self.state.update(phase="idle")

    def _ensure_categories(self, client: Client, pool: ThreadPoolExecutor, force=False):
        if self.store.categories() and not force:
            return
        self._set_phase("categories", message="Building category list from the city/letter indexes…")

        def progress(done, total, found):
            self.state["message"] = f"Category index pages {done}/{total} — {found} categories found"

        cats = build_categories(client, pool, progress)
        if not cats:
            raise RuntimeError("Category list came back empty")
        self.store.save_categories(cats)

    def _work(self, run_id: int, details_limit: int | None):
        store = self.store
        run = store.run(run_id)
        self.state.update(run_id=run_id)
        client = Client(self._stop, delay=(run["delay_min"], run["delay_max"]))
        conc = max(1, int(run["concurrency"] or DEFAULT_CONCURRENCY))
        # One pool for all requests of the run (sessions are per worker thread, so they and their tokens are
        # reused). Categories run side by side on a second, small pool: each category thread only reads its
        # landing page and then waits on its own region / city / detail requests in the shared pool.
        pool = ThreadPoolExecutor(conc, thread_name_prefix="b144-worker")
        cat_pool = ThreadPoolExecutor(category_parallelism(conc), thread_name_prefix="b144-cat")
        details = DetailQueue(client, store, run_id, pool) if run["fetch_details"] else None
        self._details = details
        try:
            self._ensure_categories(client, pool)
            if run["all_categories"]:
                store.ensure_run_categories(run_id, [c["cat_code"] for c in store.categories()])

            pending = store.pending_categories(run_id)
            self._set_phase("scraping", self._work_units(run)[0])
            # Listings and details run together: each saved page of listings queues its businesses' detail
            # pages at once, and a category counts as finished only when its details are in, so every finished
            # category is complete in the Excel (phones included) even if the run is stopped part-way.
            live = details if details is not None and not details_limit else None
            if live is not None:
                backlog = store.mids_missing_details(run_id)  # e.g. a resumed run, or details switched on later
                if backlog:
                    log.info("Queued details for %d businesses listed earlier in this run", len(backlog))
                    live.add(backlog)
            futures = [cat_pool.submit(self._category, client, run, cat, pool, live) for cat in pending]
            try:
                for fut in as_completed(futures):
                    fut.result()  # per-category errors are recorded inside; only Stop / WAF block get here
            except (StopRequested, WafBlocked):
                for f in futures:
                    f.cancel()
                raise

            if details is not None:
                # Catch-up: detail pages that failed earlier (retried once more), or a --details-limit test run.
                mids = store.mids_missing_details(run_id, details_limit)
                if mids:
                    c = store.counters(run_id)
                    self._set_phase("details", c["details_done"], f"Details: {len(mids):,} businesses left")
                    log.info("Fetching details for %d businesses", len(mids))
                    details.wait(details.add(mids, retry_failed=True))

            self._set_phase("export", message="Writing Excel…")
            store.set_run(run_id, status="finished", finished_at=now())
            path = export_run(store, run_id)
            store.set_run(run_id, output_path=str(path))
            c = store.counters(run_id)
            log.info("Run %d finished: %d rows, %d businesses, %d errors → %s", run_id, c["listings"],
                     c["businesses"], c["errors"], path.name)
        except (StopRequested, WafBlocked) as e:
            # A WAF block inside a detail task stops the run through the stop event, so check for it here too.
            blocked = e if isinstance(e, WafBlocked) else (details.fatal if details else None)
            if blocked:
                store.set_run(run_id, status="stopped", note=f"Paused: {blocked}")
                log.error("Run %d paused — %s", run_id, blocked)
            else:
                store.set_run(run_id, status="stopped", note="Stopped by user")
                log.info("Run %d stopped. Press Resume to continue where it left off.", run_id)
        except Exception as e:
            store.set_run(run_id, status="error", note=f"{type(e).__name__}: {e}")
            log.error("Run %d crashed: %s\n%s", run_id, e, traceback.format_exc())
        finally:
            self._stop.set()  # make any worker still in flight bail out quickly
            pool.shutdown(wait=True, cancel_futures=True)
            cat_pool.shutdown(wait=True, cancel_futures=True)
            client.close()
            self._details = None
            self.state.update(phase="idle", message="")

    def _category(self, client: Client, run: dict, cat: dict, pool: ThreadPoolExecutor,
                  details: DetailQueue | None):
        store, run_id = self.store, run["id"]
        client.check_stop()
        with self._active_lock:
            self._active[cat["cat_code"]] = cat["name"]
        try:
            scrape_category(client, store, run_id, cat, pool, bool(run["city_sweep"]),
                            on_rows=details.add_rows if details else None)
            if details is not None:
                # Queue anything of this category still missing (e.g. listed before a restart) and wait for all of
                # it, so this thread only moves on to a new category once this one has its phones.
                details.wait(details.add(store.mids_missing_details(run_id, cat_code=cat["cat_code"])))
        except (StopRequested, WafBlocked):
            raise
        except Exception as e:
            if self._stop.is_set():  # a request cancelled by Stop, not a real failure
                raise StopRequested() from e
            store.add_error(run_id, cat["name"], f"{type(e).__name__}: {e}")
            store.finish_category(run_id, cat["cat_code"], "error", error=str(e)[:2000])
            log.error("[%s] failed: %s", cat["name"], e)
        finally:
            with self._active_lock:
                self._active.pop(cat["cat_code"], None)

    def export_now(self, run_id: int | None = None) -> Path:
        run = self.store.run(run_id) if run_id else self.store.latest_run()
        if not run:
            raise RuntimeError("Nothing to export yet")
        path = export_run(self.store, run["id"])
        if not self.is_running():
            self.store.set_run(run["id"], output_path=str(path))
        return path
