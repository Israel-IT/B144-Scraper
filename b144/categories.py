"""Build the full category list.

Sources, unioned by catCode:
  * /indexes/cities/ -> 115 cities -> /indexes/{city}/ -> CategoriesTbl ("X בעיר", "/X/עיר/")
  * /indexes/{letter}/ for each Hebrew letter -> CategoriesTbl ("X", "/X/")
(/indexes/categories/{letter}/ is broken on the site: it always returns the "א" list.)
"""

import logging
import urllib.parse as up
from concurrent.futures import Executor, as_completed

from . import parse
from .http import Client, StopRequested

log = logging.getLogger("b144")

LETTERS = list("אבגדהוזחטיכלמנסעפצקרשת")


def build_categories(client: Client, pool: Executor, progress=None) -> list[dict]:
    cities_page = client.get_page("/indexes/cities/")
    cities = (cities_page.props.get("CitiesListsTbl") if cities_page else None) or []
    log.info("Category index: %d cities, %d letters", len(cities), len(LETTERS))

    jobs = [(f"/indexes/{letter}/", None) for letter in LETTERS]
    jobs += [(up.unquote(c["link"]), (c.get("catDesc") or "").strip()) for c in cities if c.get("link")]

    found: dict[str, dict] = {}
    names_from_letters: set[str] = set()

    def fetch(job):
        path, city_name = job
        page = client.get_page(path)
        return job, ((page.props.get("CategoriesTbl") if page else None) or [])

    done = 0
    futures = [pool.submit(fetch, j) for j in jobs]
    try:
        for fut in as_completed(futures):
            try:
                (path, city_name), rows = fut.result()
            except StopRequested:
                raise
            except Exception as e:
                log.error("Category index page failed: %s", e)
                continue
            for row in rows:
                code = str(row.get("catCode") or "").strip()
                slug = parse.slug_of(row.get("link") or "")
                if not code or not slug:
                    continue
                desc = (row.get("catDesc") or "").strip()
                name = parse.strip_city_suffix(desc, city_name) if city_name else desc
                if city_name is None:
                    names_from_letters.add(code)
                    found[code] = {"cat_code": code, "name": name, "slug": slug}
                elif code not in found:
                    found[code] = {"cat_code": code, "name": name or slug.replace("-", " "), "slug": slug}
            done += 1
            if progress:
                progress(done, len(jobs), len(found))
    except StopRequested:
        for f in futures:
            f.cancel()
        raise

    cats = sorted(found.values(), key=lambda c: c["name"])
    log.info("Category list built: %d unique categories (%d also in the A–Z index)", len(cats), len(names_from_letters))
    return cats
