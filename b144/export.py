"""Excel export: Businesses (one row per category+business), Summary, Run info. Right-to-left sheets."""

import json
from datetime import datetime
from pathlib import Path

import xlsxwriter

from . import DATA_DIR
from .store import Store

MAX_CELL = 32_000  # Excel's hard limit is 32,767 characters per cell

# (header, width, getter(listing, detail, row_meta))
COLUMNS = [
    ("Category", 22, lambda l, d, m: m["cat_name"]),
    ("Category code", 10, lambda l, d, m: m["cat_code"]),
    ("Business name", 32, lambda l, d, m: d.get("name") or l.get("Name")),
    ("Main phone", 16, lambda l, d, m: d.get("main_phone") or _unmasked(l.get("Phone"))),
    ("Mobile phone", 14, lambda l, d, m: _mobile(d)),
    ("Other phones", 18, lambda l, d, m: d.get("other_phones")),
    ("Email", 22, lambda l, d, m: d.get("email")),
    ("Fax", 13, lambda l, d, m: d.get("fax")),
    ("WhatsApp (Y/N)", 9, lambda l, d, m: _yn(d.get("whatsapp") if d else l.get("isWhatsapp"))),
    ("Website", 28, lambda l, d, m: d.get("website") or l.get("Web")),
    ("Facebook", 28, lambda l, d, m: d.get("facebook") or l.get("FAddress")),
    ("Instagram", 28, lambda l, d, m: d.get("instagram") or l.get("InstagramAddress")),
    ("Other links", 28, lambda l, d, m: d.get("other_links")),
    ("Street", 18, lambda l, d, m: d.get("street") or l.get("Street")),
    ("Street no.", 8, lambda l, d, m: d.get("street_no") or l.get("Street_No")),
    ("City", 14, lambda l, d, m: d.get("city") or l.get("City")),
    ("Zip", 9, lambda l, d, m: d.get("zip")),
    ("Full address", 30, lambda l, d, m: d.get("address")),
    ("Service location", 26, lambda l, d, m: l.get("Service_Location")),
    ("Regions served", 30, lambda l, d, m: d.get("regions_served")),
    ("Subcategories", 30, lambda l, d, m: d.get("subcategories")),
    ("Additional services/tags", 30, lambda l, d, m: d.get("additionals")),
    ("Languages", 14, lambda l, d, m: d.get("languages")),
    ("Opening hours", 30, lambda l, d, m: d.get("opening_hours")),
    ("Rating avg", 8, lambda l, d, m: _num(d.get("rating_avg") if d.get("rating_avg") is not None else l.get("Rating_avg"))),
    ("Rating count", 8, lambda l, d, m: _num(d.get("rating_count") if d.get("rating_count") is not None else l.get("Rating_count"))),
    ("Description", 50, lambda l, d, m: d.get("description") or l.get("Description")),
    ("Latitude", 11, lambda l, d, m: _num(d.get("lat") or l.get("MapY"))),
    ("Longitude", 11, lambda l, d, m: _num(d.get("lon") or l.get("MapX"))),
    ("B144 page URL", 30, lambda l, d, m: f"https://www.b144.co.il/b144_sip/{m['mid']}/"),
    ("Member ID", 18, lambda l, d, m: d.get("member_id") or l.get("Member_ID")),
    ("MID", 18, lambda l, d, m: m["mid"]),
    ("Scraped at", 18, lambda l, d, m: m["scraped_at"]),
    # N = only the category list row was scraped: B144 masks phone numbers there, so phones are mostly empty.
    ("Full details (Y/N)", 9, lambda l, d, m: "Y" if d else "N"),
]


def _unmasked(phone):
    phone = (phone or "").strip()
    return "" if phone.startswith("...") else phone


def _mobile(d: dict) -> str:
    """B144's own mobile field, else the first Israeli mobile (05x) among the business's other numbers."""
    if d.get("mobile_phone"):
        return d["mobile_phone"]
    for phone in [d.get("main_phone") or ""] + (d.get("other_phones") or "").split(","):
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits.startswith("05") and len(digits) == 10:
            return phone.strip()
    return ""


def _yn(value):
    if value is None or value == "":
        return ""
    return "Y" if value else "N"


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def default_path() -> Path:
    return DATA_DIR / f"b144_businesses_{datetime.now():%Y%m%d_%H%M}.xlsx"


def export_run(store: Store, run_id: int, path: Path | None = None) -> Path:
    path = Path(path or default_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = store.reader()
    run = dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True, "strings_to_urls": False,
                                         "strings_to_formulas": False, "strings_to_numbers": False})
    head = wb.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1, "text_wrap": True, "valign": "top"})

    # ---- Businesses --------------------------------------------------------------
    ws = wb.add_worksheet("Businesses")
    ws.right_to_left()
    for col, (title, width, _) in enumerate(COLUMNS):
        ws.set_column(col, col, width)
        ws.write_string(0, col, title, head)
    ws.freeze_panes(1, 0)

    rows = conn.execute(
        "SELECT l.cat_code, COALESCE(c.name, l.cat_code) cat_name, l.mid, l.data, l.scraped_at, d.data detail "
        "FROM listings l LEFT JOIN categories c USING(cat_code) LEFT JOIN details d ON d.mid=l.mid "
        "WHERE l.run_id=? ORDER BY cat_name, l.rowid", (run_id,))
    summary: dict[str, dict] = {}
    n = 0
    for r in rows:
        listing = json.loads(r["data"]) if r["data"] else {}
        detail = json.loads(r["detail"]) if r["detail"] else {}
        meta = {"cat_code": r["cat_code"], "cat_name": r["cat_name"], "mid": r["mid"], "scraped_at": r["scraped_at"]}
        n += 1
        values = []
        for col, (_, _, get) in enumerate(COLUMNS):
            value = get(listing, detail, meta)
            values.append(value)
            if value is None or value == "":
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                ws.write_number(n, col, value)
            else:
                ws.write_string(n, col, str(value)[:MAX_CELL])
        s = summary.setdefault(r["cat_code"], {"name": r["cat_name"], "count": 0, "mobile": 0, "web": 0})
        s["count"] += 1
        s["mobile"] += bool(values[4])
        s["web"] += bool(values[9])
    ws.autofilter(0, 0, max(n, 1), len(COLUMNS) - 1)

    # ---- Summary -------------------------------------------------------------------
    progress = {p["cat_code"]: dict(p) for p in conn.execute(
        "SELECT * FROM cat_progress WHERE run_id=?", (run_id,))}
    ss = wb.add_worksheet("Summary")
    ss.right_to_left()
    heads = [("Category", 30), ("catCode", 10), ("Businesses", 11), ("# with mobile", 12), ("# with website", 12),
             ("Listed on site", 12), ("Σ region counts", 12), ("Found via city sweep", 12), ("Status", 10)]
    for col, (title, width) in enumerate(heads):
        ss.set_column(col, col, width)
        ss.write_string(0, col, title, head)
    ss.freeze_panes(1, 0)
    names = {c["cat_code"]: c["name"] for c in conn.execute("SELECT cat_code,name FROM categories")}
    city_rows = {r[0]: r[1] for r in conn.execute(
        "SELECT cat_code, COUNT(*) FROM listings WHERE run_id=? AND region LIKE 'city:%' GROUP BY cat_code",
        (run_id,))}
    codes = sorted(set(summary) | set(progress), key=lambda c: summary.get(c, {}).get("name") or names.get(c, c))
    for i, code in enumerate(codes, start=1):
        s = summary.get(code, {"name": names.get(code, code), "count": 0, "mobile": 0, "web": 0})
        p = progress.get(code, {})
        ss.write_string(i, 0, s["name"] or "")
        ss.write_string(i, 1, code)
        ss.write_number(i, 2, s["count"])
        ss.write_number(i, 3, s["mobile"])
        ss.write_number(i, 4, s["web"])
        if p.get("site_total") is not None:
            ss.write_number(i, 5, p["site_total"])
        if p.get("region_total") is not None:
            ss.write_number(i, 6, p["region_total"])
        ss.write_number(i, 7, city_rows.get(code, 0))
        ss.write_string(i, 8, p.get("status") or "")
    ss.autofilter(0, 0, max(len(codes), 1), len(heads) - 1)

    # ---- Run info ------------------------------------------------------------------------
    ri = wb.add_worksheet("Run info")
    ri.set_column(0, 0, 28)
    ri.set_column(1, 1, 90)
    unique_mids = conn.execute("SELECT COUNT(DISTINCT mid) FROM listings WHERE run_id=?", (run_id,)).fetchone()[0]
    with_details = conn.execute(
        "SELECT COUNT(*) FROM details WHERE status='ok' AND mid IN (SELECT mid FROM listings WHERE run_id=?)",
        (run_id,)).fetchone()[0]
    errors = conn.execute("SELECT ts, context, message FROM errors WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    done = sum(1 for p in progress.values() if p["status"] == "done")
    info = [
        ("Run ID", str(run_id)),
        ("Started", run.get("started_at") or ""),
        ("Finished", run.get("finished_at") or "(not finished — partial export)"),
        ("Status", run.get("status") or ""),
        ("Exported at", datetime.now().isoformat(timespec="seconds")),
        ("Categories in run", str(len(progress))),
        ("Categories completed", str(done)),
        ("Rows (category × business)", str(n)),
        ("Unique businesses", str(unique_mids)),
        ("Businesses with full details", str(with_details)),
        ("Details fetched", "yes" if run.get("fetch_details") else "no (listing data only)"),
        ("City sweep", "yes" if run.get("city_sweep") else "no"),
        ("Found via city sweep", str(sum(city_rows.values()))),
        ("Errors", str(len(errors))),
        ("Source", "https://www.b144.co.il/"),
        ("Note", "A main phone starting with 076 is B144's call-tracking number (it forwards to the business); "
                 "the business's own numbers are then in Mobile phone / Other phones when listed. Rows with "
                 "Full details = N come from category lists only, where B144 masks phone numbers."),
    ]
    for i, (k, v) in enumerate(info):
        ri.write_string(i, 0, k, head)
        ri.write_string(i, 1, v)
    base = len(info) + 1
    if errors:
        ri.write_string(base, 0, "Error log", head)
        for j, e in enumerate(errors[:5000], start=base + 1):
            ri.write_string(j, 0, e["ts"] or "")
            ri.write_string(j, 1, f"{e['context']}: {e['message']}"[:MAX_CELL])

    wb.close()
    conn.close()
    return path
