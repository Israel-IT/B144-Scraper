"""Collect every listing of one category across the whole country.

  GET /{slug}/                 -> national ("ארצי") rows, the API's Category string, region links
  per region /{slug}/אזור-…/   -> CityCode (cached per region slug) + page 1
  POST getSearch PageIndex 2.. -> remaining pages, 15 rows each, until TotalCount / empty page
  city sweep (optional)        -> for regions where the site's area count exceeds what the region list gave,
                                  read the top "serves this city" group of each city page in that region

Why the sweep: businesses that list individual cities as their service area (rather than whole regions)
are left out of the regional lists but appear at the top of those cities' pages.

Every page is saved together with the region cursor, and every swept city is recorded, so an interrupted
run resumes where it stopped.
"""

import json
import logging
import math
import re
from collections.abc import Callable
from concurrent.futures import Executor, as_completed

from . import parse
from .http import Client, FetchError, StopRequested, WafBlocked, search_payload
from .store import Store

log = logging.getLogger("b144")

PAGE_SIZE = 15
MAX_PAGES = 3000  # safety cap per region


def scrape_category(client: Client, store: Store, run_id: int, cat: dict, pool: Executor,
                    city_sweep: bool = False, on_rows: Callable[[list[dict]], None] | None = None) -> dict:
    """`on_rows` gets every page of rows as soon as it is saved (the runner queues their detail pages)."""
    on_rows = on_rows or (lambda rows: None)
    code, slug = cat["cat_code"], cat["slug"]
    landing = client.get_page(f"/{slug}/")
    if landing is None:
        log.warning("[%s] landing page /%s/ not found — skipping", cat["name"], slug)
        store.finish_category(run_id, code, "done", site_total=0, error="landing page not found")
        return {"site_total": 0, "rows": 0}

    props = landing.props
    rows = props.get("searchObj") or []
    seo = props.get("seoAnalyzerObj") or {}
    api_category = seo.get("Category") or cat["name"]
    api_cat_code = str(seo.get("Category_Code") or code)
    if api_category and api_category != cat["name"]:
        store.rename_category(code, api_category)
    site_total = int(rows[0].get("TotalCountAllResults") or 0) if rows else 0
    store.set_site_total(run_id, code, site_total)

    national = [r for r in rows if (r.get("AreaName") or "").strip() == parse.NATIONAL]
    if national and not (store.region_progress(run_id, code, parse.NATIONAL) or {}).get("done"):
        store.save_page(run_id, code, parse.NATIONAL, 1, national,
                        total=int(national[0].get("TotalCount") or len(national)), done=True)
        on_rows(national)
        shown, expected = len(national), int(national[0].get("TotalCount") or 0)
        if expected > shown:
            log.warning("[%s] %d national listings but only %d shown on the landing page", api_category, expected, shown)

    regions = parse.region_slugs(landing.body, props, slug)
    if not regions and site_total > len(national):
        regions = store.known_regions()  # no links found; try every region we know about
    log.info("[%s] %d listings on site, %d national, %d regions", api_category, site_total, len(national), len(regions))

    ctx = {"slug": slug, "code": code, "api_category": api_category, "api_cat_code": api_cat_code, "seo": seo,
           "on_rows": on_rows}
    errors: list[str] = []
    futures = {pool.submit(scrape_region, client, store, run_id, ctx, r): r for r in regions}
    try:
        for fut in as_completed(futures):
            try:
                fut.result()
            except (StopRequested, WafBlocked):
                raise
            except Exception as e:
                errors.append(f"{futures[fut]}: {e}")
                store.add_error(run_id, f"{api_category} / {futures[fut]}", str(e))
                log.error("[%s] region %s failed: %s", api_category, futures[fut], e)
    except (StopRequested, WafBlocked):
        for f in futures:
            f.cancel()
        raise

    if city_sweep and not errors:
        errors += sweep_cities(client, store, run_id, ctx, props, pool)

    status = "error" if errors else "done"
    store.finish_category(run_id, code, status, site_total=site_total, error="; ".join(errors)[:2000] or None)
    return {"site_total": site_total, "errors": errors}


def scrape_region(client: Client, store: Store, run_id: int, ctx: dict, region: str):
    code, slug = ctx["code"], ctx["slug"]
    prog = store.region_progress(run_id, code, region) or {}
    if prog.get("done"):
        return
    next_page = int(prog.get("last_page") or 0) + 1
    total = prog.get("total")
    city_code = prog.get("city_code") or store.region_code(region)

    if city_code is None:
        # Unknown region code: the server-rendered region page gives us the code and page 1.
        page = client.get_page(f"/{slug}/{region}/")
        if page is None:
            store.save_page(run_id, code, region, 0, [], total=0, done=True)
            return
        rseo = page.props.get("seoAnalyzerObj") or {}
        city_code = str(rseo.get("CityCode") or "")
        if not city_code:
            raise FetchError(f"no CityCode on region page {region}")
        store.set_region_code(region, city_code)
        if next_page == 1:
            rows = page.props.get("searchObj") or []
            total = int(rows[0].get("TotalCount") or 0) if rows else 0
            store.save_page(run_id, code, region, 1, rows, total=total, city_code=city_code, done=not rows)
            ctx["on_rows"](rows)
            next_page = 2

    while next_page <= MAX_PAGES:
        if total is not None and (next_page - 1) * PAGE_SIZE >= int(total):
            break
        js = search_payload(ctx["seo"], next_page, category=ctx["api_category"], city=region,
                            city_code=city_code, cat_code=ctx["api_cat_code"])
        rows = client.search(js)
        if not rows:
            break
        total = int(rows[0].get("TotalCount") or total or 0)
        store.save_page(run_id, code, region, next_page, rows, total=total, city_code=city_code)
        ctx["on_rows"](rows)
        next_page += 1

    store.save_page(run_id, code, region, next_page - 1, [], total=total, city_code=city_code, done=True)
    pages = math.ceil((total or 0) / PAGE_SIZE)
    log.debug("[%s] %s: %s listings, %d pages", ctx["api_category"], region, total, pages)


# ---- city sweep ------------------------------------------------------------------------------------------
def city_slug(name: str) -> str:
    """'נח"ל אבנת' -> 'נחל-אבנת', 'אבו גוש ק.יערים' -> 'אבו-גוש-קיערים' (the site's URL form)."""
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", name.strip()))


def index_cities(client: Client, store: Store) -> list[dict]:
    """The 115 cities of /indexes/cities/ as {slug, code}, cached in the meta table."""
    cached = store.get_meta("index_cities")
    if cached:
        return json.loads(cached)
    page = client.get_page("/indexes/cities/")
    cities = [{"slug": parse.slug_of(c["link"].replace("/indexes/", "/")), "code": str(c.get("cityCode"))}
              for c in ((page.props.get("CitiesListsTbl") if page else None) or []) if c.get("link")]
    if cities:
        store.set_meta("index_cities", json.dumps(cities, ensure_ascii=False))
    return cities


def sweep_cities(client: Client, store: Store, run_id: int, ctx: dict, props: dict, pool: Executor) -> list[str]:
    """Visit the cities of every region whose area count on the landing page is higher than its region list.
    Returns error strings (empty when every city was swept)."""
    code, slug = ctx["code"], ctx["slug"]
    areas = {}
    for area in ((props.get("filtersData") or {}).get("areas") or []):
        name = (area.get("areaName") or "").strip()
        if name.startswith("אזור"):
            areas[re.sub(r"\s+", "-", name)] = int(area.get("membersCount") or 0)
    totals = store.region_totals(run_id, code)
    gap_regions = [r for r, n in areas.items() if n > totals.get(r, 0)]
    if not gap_regions:
        return []

    known = index_cities(client, store)
    cities: dict[str, str] = {}  # city slug -> region slug
    for region in gap_regions:
        page = client.get_page(f"/{slug}/{region}/")
        if page is None:
            continue
        codes = {str(c) for c in page.props.get("citiesCodes") or []}
        for name in ((page.props.get("filtersData") or {}).get("cities") or []):
            cities.setdefault(city_slug(name), region)
        for city in known:
            if city["code"] in codes:
                cities.setdefault(city["slug"], region)
    todo = [(c, r) for c, r in cities.items() if not store.city_done(run_id, code, c)]
    log.info("[%s] city sweep: %d regions with a gap, %d cities (%d left)",
             ctx["api_category"], len(gap_regions), len(cities), len(todo))

    errors: list[str] = []
    new = 0
    futures = {pool.submit(sweep_city, client, store, run_id, ctx, c, r): c for c, r in todo}
    try:
        for fut in as_completed(futures):
            try:
                new += fut.result()
            except (StopRequested, WafBlocked):
                raise
            except Exception as e:
                errors.append(f"city {futures[fut]}: {e}")
                store.add_error(run_id, f"{ctx['api_category']} / city {futures[fut]}", str(e))
                log.error("[%s] city %s failed: %s", ctx["api_category"], futures[fut], e)
    except (StopRequested, WafBlocked):
        for f in futures:
            f.cancel()
        raise
    if new:
        log.info("[%s] city sweep found %d businesses missing from the regional lists", ctx["api_category"], new)
    return errors


def scrape_city(client: Client, store: Store, ctx: dict, city: str) -> list[dict] | None:
    """Rows from the top of a city page, up to the first business located in the city
    (those are already covered by the regional lists). None if the city page doesn't exist."""
    city_code = store.region_code(city)
    if city_code:
        rows = client.search(search_payload(ctx["seo"], 1, category=ctx["api_category"], city=city,
                                            city_code=city_code, cat_code=ctx["api_cat_code"]))
    else:
        page = client.get_page(f"/{ctx['slug']}/{city}/")
        if page is None:
            return None
        seo = page.props.get("seoAnalyzerObj") or {}
        city_code = str(seo.get("CityCode") or "")
        if not city_code or seo.get("City") != city:
            return None  # slug didn't resolve to this city (redirect or unknown place)
        store.set_region_code(city, city_code)
        rows = list(page.props.get("searchObj") or [])

    page_index = 2
    while rows and not any(r.get("isbyCity") == 1 for r in rows) \
            and len(rows) < int(rows[0].get("TotalCountAllResults") or 0) and page_index <= 20:
        more = client.search(search_payload(ctx["seo"], page_index, category=ctx["api_category"], city=city,
                                            city_code=city_code, cat_code=ctx["api_cat_code"]))
        if not more:
            break
        rows += more
        page_index += 1
    return rows


def sweep_city(client: Client, store: Store, run_id: int, ctx: dict, city: str, region: str) -> int:
    rows = scrape_city(client, store, ctx, city) or []
    new = store.save_city(run_id, ctx["code"], city, region, rows)
    ctx["on_rows"](rows)
    return new
