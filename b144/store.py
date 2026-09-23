"""SQLite state: categories, per-run progress, listings and the per-MID details cache.

Progress and listings are scoped to a run so that "Run" starts fresh and "Resume" continues.
Details are cached globally per MID, so a business is fetched once across categories and runs.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS categories (
    cat_code TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL, updated_at TEXT
);
-- slug -> CityCode for regions ("אזור-…", codes -100…-114) and, from the city sweep, cities
CREATE TABLE IF NOT EXISTS region_codes (region_slug TEXT PRIMARY KEY, city_code TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    status TEXT,                    -- running | stopped | interrupted | error | finished | abandoned
    all_categories INTEGER, fetch_details INTEGER,
    concurrency INTEGER, delay_min REAL, delay_max REAL,
    output_path TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS cat_progress (
    run_id INTEGER, cat_code TEXT,
    status TEXT DEFAULT 'pending',  -- pending | done | error
    site_total INTEGER, region_total INTEGER, rows INTEGER, error TEXT, finished_at TEXT,
    PRIMARY KEY (run_id, cat_code)
);
CREATE TABLE IF NOT EXISTS region_progress (
    run_id INTEGER, cat_code TEXT, region_slug TEXT,
    city_code TEXT, total INTEGER, last_page INTEGER DEFAULT 0, fetched INTEGER DEFAULT 0, done INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, cat_code, region_slug)
);
CREATE TABLE IF NOT EXISTS city_progress (
    run_id INTEGER, cat_code TEXT, city_slug TEXT, region_slug TEXT, rows INTEGER DEFAULT 0, done INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, cat_code, city_slug)
);
CREATE TABLE IF NOT EXISTS listings (
    run_id INTEGER, cat_code TEXT, mid TEXT, region TEXT, data TEXT, scraped_at TEXT,
    PRIMARY KEY (run_id, cat_code, mid)
);
CREATE INDEX IF NOT EXISTS listings_mid ON listings (run_id, mid);
CREATE TABLE IF NOT EXISTS details (mid TEXT PRIMARY KEY, status TEXT, data TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS errors (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts TEXT, context TEXT, message TEXT);
"""

UNFINISHED = ("stopped", "interrupted", "error", "running")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def listing_key(row: dict) -> str:
    return str(row.get("MID") or row.get("Member_UniqueId") or row.get("Member_ID") or "")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(runs)")}
        if "city_sweep" not in cols:
            self.conn.execute("ALTER TABLE runs ADD COLUMN city_sweep INTEGER DEFAULT 0")

    def reader(self) -> sqlite3.Connection:
        """Separate read-only connection (used by export so it doesn't block the scraper)."""
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False, timeout=60)
        conn.row_factory = sqlite3.Row
        return conn

    def _q(self, sql, args=()):
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _x(self, sql, args=()):
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # ---- meta / categories --------------------------------------------------
    def get_meta(self, key, default=None):
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self._x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    def save_categories(self, cats: list[dict]):
        ts = now()
        with self._lock:
            self.conn.executemany(
                "INSERT INTO categories(cat_code,name,slug,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(cat_code) DO UPDATE SET name=excluded.name, slug=excluded.slug, updated_at=excluded.updated_at",
                [(c["cat_code"], c["name"], c["slug"], ts) for c in cats],
            )
            self.conn.commit()
        self.set_meta("categories_built_at", ts)

    def rename_category(self, cat_code, name):
        self._x("UPDATE categories SET name=? WHERE cat_code=?", (name, cat_code))

    def categories(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT cat_code,name,slug FROM categories ORDER BY name")]

    def category(self, cat_code) -> dict | None:
        row = self._one("SELECT cat_code,name,slug FROM categories WHERE cat_code=?", (cat_code,))
        return dict(row) if row else None

    def find_categories(self, term: str) -> list[dict]:
        term = term.strip().strip("/")
        return [dict(r) for r in self._q(
            "SELECT cat_code,name,slug FROM categories WHERE cat_code=? OR name=? OR slug=?", (term, term, term))]

    # ---- region codes -------------------------------------------------------
    def region_code(self, region_slug) -> str | None:
        row = self._one("SELECT city_code FROM region_codes WHERE region_slug=?", (region_slug,))
        return row["city_code"] if row else None

    def set_region_code(self, region_slug, city_code):
        self._x("INSERT OR REPLACE INTO region_codes(region_slug,city_code) VALUES(?,?)", (region_slug, str(city_code)))

    def known_regions(self) -> list[str]:
        return [r["region_slug"] for r in self._q(
            "SELECT region_slug FROM region_codes WHERE region_slug LIKE 'אזור-%' ORDER BY city_code DESC")]

    # ---- runs -----------------------------------------------------------------
    def create_run(self, cat_codes: list[str] | None, fetch_details, concurrency, delay, city_sweep=True) -> int:
        with self._lock:
            self.conn.execute("UPDATE runs SET status='abandoned' WHERE status IN (?,?,?,?)", UNFINISHED)
            cur = self.conn.execute(
                "INSERT INTO runs(started_at,status,all_categories,fetch_details,concurrency,delay_min,delay_max,"
                "city_sweep) VALUES(?,?,?,?,?,?,?,?)",
                (now(), "running", int(cat_codes is None), int(bool(fetch_details)), int(concurrency),
                 float(delay[0]), float(delay[1]), int(bool(city_sweep))),
            )
            run_id = cur.lastrowid
            if cat_codes:
                self.conn.executemany("INSERT OR IGNORE INTO cat_progress(run_id,cat_code) VALUES(?,?)",
                                      [(run_id, c) for c in cat_codes])
            self.conn.commit()
        return run_id

    def ensure_run_categories(self, run_id, cat_codes: list[str]):
        with self._lock:
            self.conn.executemany("INSERT OR IGNORE INTO cat_progress(run_id,cat_code) VALUES(?,?)",
                                  [(run_id, c) for c in cat_codes])
            self.conn.commit()

    def run(self, run_id) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        return dict(row) if row else None

    def latest_run(self) -> dict | None:
        row = self._one("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    def unfinished_run(self) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE status IN (?,?,?,?) ORDER BY id DESC LIMIT 1", UNFINISHED)
        return dict(row) if row else None

    def last_output(self) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE output_path IS NOT NULL ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    def set_run(self, run_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def mark_interrupted(self):
        """At process start nothing can be running: any 'running' run was killed mid-way."""
        self._x("UPDATE runs SET status='interrupted' WHERE status='running'")

    # ---- category / region progress -------------------------------------------
    def pending_categories(self, run_id) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT c.cat_code, c.name, c.slug FROM cat_progress p JOIN categories c USING(cat_code) "
            "WHERE p.run_id=? AND p.status!='done' ORDER BY c.name", (run_id,))]

    def run_category_codes(self, run_id) -> list[str]:
        return [r["cat_code"] for r in self._q("SELECT cat_code FROM cat_progress WHERE run_id=?", (run_id,))]

    def finish_category(self, run_id, cat_code, status, site_total=None, error=None):
        with self._lock:
            region_total = self.conn.execute(
                "SELECT COALESCE(SUM(total),0) FROM region_progress WHERE run_id=? AND cat_code=?",
                (run_id, cat_code)).fetchone()[0]
            rows = self.conn.execute("SELECT COUNT(*) FROM listings WHERE run_id=? AND cat_code=?",
                                     (run_id, cat_code)).fetchone()[0]
            self.conn.execute(
                "UPDATE cat_progress SET status=?, site_total=COALESCE(?,site_total), region_total=?, rows=?, "
                "error=?, finished_at=? WHERE run_id=? AND cat_code=?",
                (status, site_total, region_total, rows, error, now(), run_id, cat_code))
            self.conn.commit()

    def set_site_total(self, run_id, cat_code, site_total):
        self._x("UPDATE cat_progress SET site_total=? WHERE run_id=? AND cat_code=?", (site_total, run_id, cat_code))

    def region_progress(self, run_id, cat_code, region_slug) -> dict | None:
        row = self._one("SELECT * FROM region_progress WHERE run_id=? AND cat_code=? AND region_slug=?",
                        (run_id, cat_code, region_slug))
        return dict(row) if row else None

    def save_page(self, run_id, cat_code, region_slug, page, rows: list[dict], total=None, city_code=None,
                  done=False):
        """Upsert one page of listing rows and advance the region cursor in one transaction."""
        ts = now()
        with self._lock:
            self.conn.executemany(
                "INSERT INTO listings(run_id,cat_code,mid,region,data,scraped_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(run_id,cat_code,mid) DO NOTHING",
                [(run_id, cat_code, listing_key(r), region_slug, json.dumps(r, ensure_ascii=False), ts)
                 for r in rows if listing_key(r)],
            )
            self.conn.execute(
                "INSERT INTO region_progress(run_id,cat_code,region_slug,city_code,total,last_page,fetched,done) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,cat_code,region_slug) DO UPDATE SET "
                "city_code=COALESCE(excluded.city_code,city_code), total=COALESCE(excluded.total,total), "
                "last_page=MAX(last_page,excluded.last_page), fetched=fetched+excluded.fetched, "
                "done=MAX(done,excluded.done)",
                (run_id, cat_code, region_slug, city_code, total, page, len(rows), int(done)),
            )
            self.conn.commit()

    def mark_region_done(self, run_id, cat_code, region_slug):
        self._x("UPDATE region_progress SET done=1 WHERE run_id=? AND cat_code=? AND region_slug=?",
                (run_id, cat_code, region_slug))

    def city_done(self, run_id, cat_code, city_slug) -> bool:
        row = self._one("SELECT done FROM city_progress WHERE run_id=? AND cat_code=? AND city_slug=?",
                        (run_id, cat_code, city_slug))
        return bool(row and row["done"])

    def save_city(self, run_id, cat_code, city_slug, region_slug, rows: list[dict]) -> int:
        """Store a city page's rows (deduped against everything already found) and mark the city done.
        Returns how many rows were new for this category."""
        ts = now()
        with self._lock:
            before = self.conn.total_changes
            self.conn.executemany(
                "INSERT INTO listings(run_id,cat_code,mid,region,data,scraped_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(run_id,cat_code,mid) DO NOTHING",
                [(run_id, cat_code, listing_key(r), f"city:{city_slug}", json.dumps(r, ensure_ascii=False), ts)
                 for r in rows if listing_key(r)],
            )
            new = self.conn.total_changes - before
            self.conn.execute(
                "INSERT INTO city_progress(run_id,cat_code,city_slug,region_slug,rows,done) VALUES(?,?,?,?,?,1) "
                "ON CONFLICT(run_id,cat_code,city_slug) DO UPDATE SET rows=excluded.rows, done=1",
                (run_id, cat_code, city_slug, region_slug, new))
            self.conn.commit()
        return new

    def region_totals(self, run_id, cat_code) -> dict[str, int]:
        return {r["region_slug"]: r["total"] or 0 for r in self._q(
            "SELECT region_slug,total FROM region_progress WHERE run_id=? AND cat_code=?", (run_id, cat_code))}

    def category_report(self, run_id) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT p.*, c.name, c.slug, (SELECT COUNT(*) FROM listings l WHERE l.run_id=p.run_id AND "
            "l.cat_code=p.cat_code AND l.region LIKE 'city:%') city_rows FROM cat_progress p JOIN categories c "
            "USING(cat_code) WHERE run_id=? ORDER BY c.name", (run_id,))]

    def region_report(self, run_id, cat_code) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM region_progress WHERE run_id=? AND cat_code=? ORDER BY total DESC", (run_id, cat_code))]

    # ---- details ----------------------------------------------------------------
    def mids_missing_details(self, run_id, limit=None, cat_code=None) -> list[str]:
        sql = ("SELECT DISTINCT l.mid FROM listings l LEFT JOIN details d ON d.mid=l.mid "
               "WHERE l.run_id=? AND d.mid IS NULL")
        args: tuple = (run_id,)
        if cat_code:
            sql += " AND l.cat_code=?"
            args += (cat_code,)
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["mid"] for r in self._q(sql, args)]

    def mids_with_details(self) -> set[str]:
        return {r["mid"] for r in self._q("SELECT mid FROM details")}

    def run_scope(self, run_id) -> dict:
        """How many categories a run covers, plus their names when there are only a few."""
        rows = self._q("SELECT c.name FROM cat_progress p JOIN categories c USING(cat_code) WHERE p.run_id=? "
                       "ORDER BY c.name LIMIT 6", (run_id,))
        total = self._one("SELECT COUNT(*) n FROM cat_progress WHERE run_id=?", (run_id,))["n"]
        return {"count": total, "names": [r["name"] for r in rows]}

    def listing_progress(self, run_id) -> dict:
        """Progress in listings: done categories count their site total, the category in progress counts the
        rows fetched so far, and categories not reached yet are estimated from the average site total."""
        with self._lock:
            c = self.conn
            p = c.execute(
                "SELECT COUNT(*) total, SUM(site_total IS NOT NULL) known, COALESCE(SUM(site_total),0) known_sum, "
                "COALESCE(SUM(CASE WHEN status='done' THEN site_total END),0) done_sum FROM cat_progress "
                "WHERE run_id=?", (run_id,)).fetchone()
            partial = c.execute(
                "SELECT COALESCE(SUM(r.fetched),0) FROM region_progress r JOIN cat_progress q "
                "ON q.run_id=r.run_id AND q.cat_code=r.cat_code WHERE r.run_id=? AND q.status!='done'",
                (run_id,)).fetchone()[0]
        known = p["known"] or 0
        avg = p["known_sum"] / known if known else 300  # ~300 listings per category in an 80-category sample
        expected = p["known_sum"] + (p["total"] - known) * avg
        done = min(p["done_sum"] + partial, expected) if expected else 0
        return {"done": int(done), "expected": int(expected)}

    def save_detail(self, mid, status, data: dict | None):
        self._x("INSERT OR REPLACE INTO details(mid,status,data,fetched_at) VALUES(?,?,?,?)",
                (mid, status, json.dumps(data, ensure_ascii=False) if data is not None else None, now()))

    def detail(self, mid) -> dict | None:
        row = self._one("SELECT * FROM details WHERE mid=?", (mid,))
        if not row:
            return None
        out = dict(row)
        out["data"] = json.loads(out["data"]) if out["data"] else None
        return out

    # ---- errors / counters ----------------------------------------------------------
    def add_error(self, run_id, context, message):
        self._x("INSERT INTO errors(run_id,ts,context,message) VALUES(?,?,?,?)", (run_id, now(), context, message))

    def errors(self, run_id, limit=50) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT ts,context,message FROM errors WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit))]

    def counters(self, run_id) -> dict:
        with self._lock:
            c = self.conn
            cats = dict(c.execute(
                "SELECT COUNT(*) total, SUM(status='done') done, SUM(status='error') err FROM cat_progress "
                "WHERE run_id=?", (run_id,)).fetchone())
            listings = c.execute("SELECT COUNT(*) FROM listings WHERE run_id=?", (run_id,)).fetchone()[0]
            mids = c.execute("SELECT COUNT(DISTINCT mid) FROM listings WHERE run_id=?", (run_id,)).fetchone()[0]
            with_details = c.execute(
                "SELECT COUNT(*) FROM details WHERE mid IN (SELECT DISTINCT mid FROM listings WHERE run_id=?)",
                (run_id,)).fetchone()[0]
            errors = c.execute("SELECT COUNT(*) FROM errors WHERE run_id=?", (run_id,)).fetchone()[0]
        return {
            "cats_total": cats["total"] or 0, "cats_done": cats["done"] or 0, "cats_error": cats["err"] or 0,
            "listings": listings, "businesses": mids, "details_done": with_details, "errors": errors,
        }
