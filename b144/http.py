"""HTTP layer: Scrapling FetcherSession (curl_cffi, Chrome TLS) with politeness, retries,
ApiToken handling and a StealthyFetcher fallback for WAF challenges.

Each worker thread gets its own session (curl sessions are not thread-safe), so each
worker also carries its own F5 cookies and ApiToken.
"""

import json
import logging
import random
import threading
import time
import urllib.parse as up
from dataclasses import dataclass

from scrapling.fetchers import FetcherSession

from . import parse

BASE = "https://www.b144.co.il"
API_URL = "https://services.b144.co.il/Services/b144SearchService.asmx/getSearch"
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}
THROTTLE_STATUSES = {403, 429}  # the site asking us to slow down: every worker pauses, not just this one

log = logging.getLogger("b144")
logging.getLogger("scrapling").setLevel(logging.WARNING)


class StopRequested(Exception):
    """The user pressed Stop."""


class WafBlocked(Exception):
    """The site served a bot challenge we could not get past."""


class FetchError(Exception):
    """A request kept failing after all retries."""


@dataclass
class Page:
    url: str
    status: int
    body: str
    props: dict


def site_url(path: str) -> str:
    """'/חשמלאים/' -> percent-encoded absolute URL."""
    if path.startswith("http"):
        return path
    return BASE + up.quote(path, safe="/")


class Client:
    def __init__(
        self,
        stop_event: threading.Event | None = None,
        delay: tuple[float, float] = (0.3, 0.8),
        retries: int = 5,
        timeout: int = 30,
    ):
        self.stop_event = stop_event or threading.Event()
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._local = threading.local()
        self._managers: list[FetcherSession] = []
        self._lock = threading.Lock()
        self._stealth_lock = threading.Lock()
        self._pause_until = 0.0  # shared cool-down after a 403/429, so a big pool backs off as a whole

    # ---- session management -------------------------------------------------
    def _session(self):
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = self._open_session()
        return sess

    def _open_session(self):
        manager = FetcherSession(impersonate="chrome", retries=1, timeout=self.timeout)
        sess = manager.__enter__()
        self._local.manager = manager
        self._local.session = sess
        with self._lock:
            self._managers.append(manager)
        return sess

    def _reset_session(self):
        manager = getattr(self._local, "manager", None)
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
            with self._lock:
                if manager in self._managers:
                    self._managers.remove(manager)
        self._local.session = None
        self._local.manager = None
        return self._open_session()

    def close(self):
        with self._lock:
            managers, self._managers = self._managers, []
        for manager in managers:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass

    def _cookie_jar(self):
        return self._session()._curl_session.cookies

    def _token(self) -> str | None:
        for cookie in self._cookie_jar().jar:
            if cookie.name == "ApiToken" and cookie.value:
                return up.unquote(cookie.value)
        return None

    # ---- politeness ---------------------------------------------------------
    def sleep(self, seconds: float):
        if self.stop_event.wait(max(0.0, seconds)):
            raise StopRequested()

    def check_stop(self):
        if self.stop_event.is_set():
            raise StopRequested()

    def _pause_all(self, seconds: float):
        with self._lock:
            self._pause_until = max(self._pause_until, time.time() + seconds)

    def _wait_cooldown(self):
        while (left := self._pause_until - time.time()) > 0:
            self.sleep(min(left, 5.0))

    # ---- core request with retries -----------------------------------------
    def _request(self, method: str, url: str, **kwargs):
        last_err = ""
        for attempt in range(self.retries):
            self.check_stop()
            self._wait_cooldown()
            self.sleep(random.uniform(*self.delay))
            try:
                resp = getattr(self._session(), method)(url, **kwargs)
            except StopRequested:
                raise
            except Exception as e:  # curl errors, timeouts, DNS…
                last_err = f"{type(e).__name__}: {e}"
            else:
                if resp.status not in RETRY_STATUSES:
                    return resp
                last_err = f"HTTP {resp.status}"
                if resp.status == 403 and attempt >= 1:
                    self._reset_session()  # F5 sometimes wants fresh cookies
            if attempt == self.retries - 1:
                break
            backoff = min(60.0, 2.0 ** (attempt + 1)) + random.random()
            if last_err in {f"HTTP {s}" for s in THROTTLE_STATUSES}:
                self._pause_all(backoff)
            log.warning("%s %s failed (%s); retry %d/%d in %.0fs",
                        method.upper(), up.unquote(url)[:120], last_err, attempt + 1, self.retries - 1, backoff)
            self.sleep(backoff)
        raise FetchError(f"{method.upper()} {up.unquote(url)} failed after {self.retries} attempts: {last_err}")

    # ---- pages ----------------------------------------------------------------
    def get_page(self, path: str) -> Page | None:
        """GET a site page and return its Next.js pageProps. None for 404 / "not found" pages."""
        url = site_url(path)
        for challenge_round in range(3):
            resp = self._request("get", url)
            if resp.status == 404:
                return None
            body = resp.body.decode("utf-8", "replace") if isinstance(resp.body, bytes) else str(resp.body)
            props = parse.page_props(body)
            if props is not None:
                if props.get("is404"):
                    return None
                return Page(url=str(resp.url), status=resp.status, body=body, props=props)
            if resp.status >= 400:
                raise FetchError(f"GET {up.unquote(url)} -> HTTP {resp.status}")
            # 200 without __NEXT_DATA__ = WAF / bot challenge
            log.warning("No __NEXT_DATA__ on %s (likely WAF challenge), round %d", up.unquote(url)[:120], challenge_round + 1)
            if challenge_round == 0:
                self._reset_session()
                self.sleep(5)
            else:
                self._stealth_prime()
        raise WafBlocked(f"Still challenged on {up.unquote(url)} after re-priming with StealthyFetcher")

    def prime(self):
        """GET the home page to (re)acquire F5 cookies and the ApiToken."""
        resp = self._request("get", BASE + "/")
        if parse.page_props(resp.body.decode("utf-8", "replace")) is None:
            self._stealth_prime()

    def _stealth_prime(self):
        """Open the site in Scrapling's stealth browser and copy its cookies into this session."""
        with self._stealth_lock:
            log.warning("Re-priming cookies with StealthyFetcher (headless browser)…")
            try:
                from scrapling.fetchers import StealthyFetcher

                page = StealthyFetcher.fetch(BASE + "/", headless=True, network_idle=True)
            except Exception as e:
                raise WafBlocked(
                    "The site is serving a bot challenge and the StealthyFetcher fallback failed "
                    f"({type(e).__name__}: {e}). If the browser is missing, run `uv run scrapling install`, "
                    "wait a while, then Resume."
                ) from e
        sess = self._reset_session()
        cookies = page.cookies or []
        items = cookies.items() if isinstance(cookies, dict) else [
            (c.get("name"), c.get("value")) for c in cookies if isinstance(c, dict)
        ]
        for name, value in items:
            if name:
                sess._curl_session.cookies.set(name, value, domain=".b144.co.il")
        if page.status and page.status >= 400:
            raise WafBlocked(f"StealthyFetcher got HTTP {page.status} from the home page")

    # ---- search API -----------------------------------------------------------
    def search(self, js_details: dict) -> list[dict]:
        """POST getSearch. Returns the Response rows ([] when past the last page)."""
        for attempt in range(4):
            token = self._token()
            if not token:
                self.prime()
                token = self._token()
                if not token:
                    raise FetchError("No ApiToken cookie after priming the session")
            resp = self._request(
                "post",
                API_URL,
                data={"jsDetails": json.dumps(js_details, ensure_ascii=False)},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": BASE,
                    "Referer": BASE + "/",
                    "Accept": "*/*",
                },
            )
            text = resp.body.decode("utf-8", "replace") if isinstance(resp.body, bytes) else str(resp.body)
            data = parse.unwrap_api(text)
            if resp.status == 200 and data and data.get("Success"):
                rows = data.get("Response")
                return rows if isinstance(rows, list) else []
            reason = (data or {}).get("ErrorMessage") or f"HTTP {resp.status}"
            log.info("Search API refused (%s); refreshing token (attempt %d)", reason, attempt + 1)
            if attempt >= 1:
                self._reset_session()
            self.prime()
        raise FetchError(f"Search API kept failing for {js_details.get('Category')}/{js_details.get('City')} "
                         f"page {js_details.get('PageIndex')}")


def search_payload(seo: dict, page_index: int, *, category: str, city: str, city_code: str, cat_code: str) -> dict:
    """Build the jsDetails object the site's own JS sends (seoAnalyzerObj + PageIndex)."""
    return {
        "isCardOpen": True,
        "OriginalDomain": "www.b144.co.il",
        "IsMobile": False,
        "Category": category,
        "City": city,
        "CityCode": str(city_code),
        "Category_Code": str(cat_code),
        "RewritePath": seo.get("RewritePath") or "",
        "IsRedirect": False,
        "Cx": "",
        "Cy": "",
        "IsCoupon": False,
        "IsOpen": False,
        "Filter": "",
        "Distance": "",
        "PageIndex": page_index,
    }
