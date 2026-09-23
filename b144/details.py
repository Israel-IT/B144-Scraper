"""Fetch /b144_sip/{MID}/ and normalize initialMemberPageData into flat, Excel-ready fields."""

import logging
import threading
from concurrent.futures import Executor, Future, wait

from . import parse
from .http import Client, StopRequested, WafBlocked
from .store import Store, listing_key

log = logging.getLogger("b144")


def _s(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _names(items) -> list[str]:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            name = _s(it.get("name") or it.get("Name") or it.get("desc"))
        else:
            name = _s(it)
        if name and name not in out:
            out.append(name)
    return out


def _links(items) -> list[str]:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            url = _s(it.get("address") or it.get("link") or it.get("url") or it.get("Url"))
            desc = _s(it.get("desc") or it.get("name"))
            if url:
                out.append(f"{desc}: {url}" if desc else url)
        elif _s(it):
            out.append(_s(it))
    return out


def _digits(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def _phone(d: dict) -> str:
    phone = _s(d.get("Phone"))
    if phone:
        return phone
    local, area = _s(d.get("phone")), _s(d.get("Area_Code"))
    if local:
        return f"{area}-{local}" if area else local
    return ""


def normalize(d: dict) -> dict:
    mobile = _s(d.get("MobilePhone"))
    main = _phone(d)
    seen = {_digits(mobile), _digits(main)}
    others = []
    for p in (_s(x) for x in d.get("MorePhoneNumbers") or []):
        if p and _digits(p) not in seen:
            seen.add(_digits(p))
            others.append(p)
    hours = d.get("MemberOpenHours") or {}
    hours_text = parse.hours_to_text(hours.get("html") or "") if isinstance(hours, dict) else _s(hours)
    if _s(d.get("RemarksOpenHours")):
        hours_text = "; ".join(x for x in (hours_text, _s(d.get("RemarksOpenHours"))) if x)
    extra_links = _links(d.get("links"))
    for label, key in (("TikTok", "TikTokAddress"), ("YouTube", "YouTubeAddress"), ("Wolt", "WoltAddress"),
                       ("Google", "GmbUrl")):
        if _s(d.get(key)):
            extra_links.append(f"{label}: {_s(d.get(key))}")
    zip_code = _s(d.get("Zip_code"))
    return {
        "name": _s(d.get("Name")),
        "main_phone": main,
        "mobile_phone": mobile,
        "other_phones": ", ".join(others),
        "fax": _s(d.get("Fax")),
        "email": _s(d.get("Email")),
        "whatsapp": bool(d.get("IsWhatsapp")),
        "website": _s(d.get("WebSite")),
        "facebook": _s(d.get("FAddress")),
        "instagram": _s(d.get("InstagramAddress")),
        "other_links": " | ".join(extra_links),
        "street": _s(d.get("Street")),
        "street_no": _s(d.get("Street_No")),
        "city": _s(d.get("City")),
        "zip": "" if zip_code in ("", "0") else zip_code,
        "address": _s(d.get("Address")),
        "regions_served": ", ".join(_names(d.get("AreasServices"))),
        "categories": ", ".join(_names(d.get("Categories"))),
        "subcategories": ", ".join(_names(d.get("SubCategories"))),
        "additionals": ", ".join(_names(d.get("Additionals"))),
        "languages": ", ".join(_names(d.get("memberLanguages"))),
        "opening_hours": hours_text,
        "rating_avg": d.get("Rating_avg"),
        "rating_count": d.get("Rating_count"),
        "description": _s(d.get("Description")),
        "lat": d.get("MapY"),
        "lon": d.get("MapX"),
        "member_id": _s(d.get("Member_ID")),
    }


def fetch_detail(client: Client, mid: str) -> tuple[str, dict | None]:
    page = client.get_page(f"/b144_sip/{mid}/")
    if page is None:
        return "not_found", None
    data = page.props.get("initialMemberPageData")
    if not data:
        return "not_found", None
    return "ok", normalize(data)


class DetailQueue:
    """Fetches business pages on the shared worker pool while the listings are still coming in.

    Listing code calls `add()` with each saved page's rows, so details start right away instead of waiting
    for the category (or the whole run) to finish listing. Every MID is fetched at most once: the details
    table is a cache across categories and runs, and MIDs already queued are skipped.
    A WAF block or Stop in any detail task stops the whole run (see `fatal`).
    """

    def __init__(self, client: Client, store: Store, run_id: int, pool: Executor):
        self.client, self.store, self.run_id, self.pool = client, store, run_id, pool
        self._lock = threading.RLock()  # RLock: a future that is already done runs its callback inside add()
        self._done: set[str] = store.mids_with_details()
        self._inflight: dict[str, Future] = {}
        self._failed: set[str] = set()
        self.fatal: BaseException | None = None

    def add(self, mids, retry_failed: bool = False) -> list[Future]:
        """Queue the MIDs that aren't fetched yet. Returns the futures covering them (new and already queued)."""
        out = []
        with self._lock:
            for mid in mids:
                mid = str(mid or "")
                if not mid or mid in self._done or (mid in self._failed and not retry_failed):
                    continue
                fut = self._inflight.get(mid)
                if fut is None:
                    self._failed.discard(mid)
                    fut = self.pool.submit(self._work, mid)
                    self._inflight[mid] = fut
                    fut.add_done_callback(lambda f, m=mid: self._finished(m, f))
                out.append(fut)
        return out

    def add_rows(self, rows: list[dict]):
        self.add(listing_key(r) for r in rows)

    def _work(self, mid):
        status, data = fetch_detail(self.client, mid)
        self.store.save_detail(mid, status, data)
        return status

    def _finished(self, mid: str, fut: Future):
        with self._lock:
            self._inflight.pop(mid, None)
            if fut.cancelled():
                return
            err = fut.exception()
            if err is None:
                self._done.add(mid)
                return
            self._failed.add(mid)
        if isinstance(err, (StopRequested, WafBlocked)):
            if isinstance(err, WafBlocked) and self.fatal is None:
                self.fatal = err
            self.client.stop_event.set()  # stop everything else too
            return
        self.store.add_error(self.run_id, f"details {mid}", str(err))
        log.error("details %s failed: %s", mid, err)

    def wait(self, futures: list[Future]):
        """Block until `futures` finish; re-raise Stop / WAF block."""
        wait(futures)
        if self.fatal is not None:
            raise self.fatal
        self.client.check_stop()

    def pending(self) -> int:
        with self._lock:
            return len(self._inflight)
