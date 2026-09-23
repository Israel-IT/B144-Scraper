"""Pure parsing helpers: Next.js page data, the XML-wrapped search API, region links."""

import html
import json
import re
import urllib.parse as up

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_API_STRING_RE = re.compile(r"<string[^>]*>(.*)</string>", re.S)

NATIONAL = "ארצי"


def page_props(body: str) -> dict | None:
    """Return `props.pageProps` from a Next.js page, or None if the page has no __NEXT_DATA__."""
    m = _NEXT_DATA_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return (data.get("props") or {}).get("pageProps") or {}


def unwrap_api(text: str) -> dict | None:
    """The search service returns JSON HTML-escaped inside an XML <string> element."""
    m = _API_STRING_RE.search(text)
    payload = html.unescape(m.group(1)) if m else text
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def slug_of(link: str) -> str:
    """'/חשמלאים/תל-אביב-יפו/' -> 'חשמלאים'."""
    parts = [p for p in up.unquote(link).split("/") if p]
    return parts[0] if parts else ""


def region_slugs(body: str, props: dict, cat_slug: str) -> list[str]:
    """Region slugs ('אזור-…') that a category landing page links to or lists in its area filter."""
    found: set[str] = set()
    text = up.unquote(body)
    for m in re.finditer(r"/" + re.escape(cat_slug) + r"/(אזור-[^/\"'?#<>\s]+)/", text):
        found.add(m.group(1))
    for area in ((props.get("filtersData") or {}).get("areas") or []):
        name = (area.get("areaName") or "").strip()
        if name.startswith("אזור"):
            found.add(re.sub(r"\s+", "-", name))
    return sorted(found)


def strip_city_suffix(cat_desc: str, city_name: str) -> str:
    """'חשמלאים בתל אביב יפו' -> 'חשמלאים'."""
    suffix = " ב" + city_name.strip()
    if cat_desc.endswith(suffix):
        return cat_desc[: -len(suffix)].strip()
    return cat_desc.strip()


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def hours_to_text(fragment: str) -> str:
    """B144 opening-hours HTML -> 'יום ראשון 09:00-18:00; יום שני …' (one entry per day/row)."""
    fragment = fragment or ""
    days = re.split(r'<div class="day"[^>]*>', fragment)[1:]
    if not days:
        days = re.split(r"<br\s*/?>|</div>", fragment, flags=re.I)
    return "; ".join(t for t in (_text(d) for d in days) if t)
