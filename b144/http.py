"""HTTP layer: Scrapling FetcherSession (curl_cffi, Chrome TLS) with politeness, retries,
ApiToken handling and a StealthyFetcher fallback for WAF challenges.

Each worker thread gets its own session (curl sessions are not thread-safe), so each
worker also carries its own F5 cookies and ApiToken.
"""

import json
import logging
import random
import threading
from collections import deque
import time
import urllib.parse as up
from dataclasses import dataclass

from scrapling.fetchers import FetcherSession

from . import parse

BASE = "https://www.b144.co.il"
API_URL = "https://services.b144.co.il/Services/b144SearchService.asmx/getSearch"
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}
THROTTLE_STATUSES = {403, 429}
# curl errors the site's firewall produces when we go too fast: (7) connection refused, (35) TLS handshake cut,
# (52) empty reply, (55)/(56) connection reset. Timeouts (28) are just a slow site and count as normal failures.
THROTTLE_CURL_CODES = ("curl: (7)", "curl: (35)", "curl: (52)", "curl: (55)", "curl: (56)")
START_ACTIVE = 20          # the throttle starts here and speeds up towards the pool size while the site is happy
RAMP_EVERY = 50            # successful requests per +1 concurrent request
GIVE_UP_AFTER = 30 * 60    # blocked this long without a single success -> pause the run (Resume later)

log = logging.getLogger("b144")
logging.getLogger("scrapling").setLevel(logging.WARNING)


class StopRequested(Exception):
    """The user pressed Stop."""


class WafBlocked(Exception):
    """The site served a bot challenge we could not get past."""


class FetchError(Exception):
    """A request kept failing after all retries."""


class Throttle:
    """How many requests may be in flight right now, shared by every worker (additive increase,
    multiplicative decrease). When the site pushes back (403/429, connection resets) the limit is halved and
    everyone pauses for a cool-down that doubles while the block lasts (15 s … 5 min). Each run of
    RAMP_EVERY successes adds one request back, up to the pool size. Above the level that last got pushed back
    it climbs ten times slower, so it settles just under what the site tolerates instead of bouncing off it."""

    def __init__(self, max_active: int):
        self.max = max(1, max_active)
        self.limit = min(self.max, START_ACTIVE)
        self.active = 0
        self._cond = threading.Condition()
        self._pause_until = 0.0
        self._cooldown = 0.0
        self._ok_streak = 0
        self._blocked_since: float | None = None
        self._ceiling = self.max + 1  # the limit at the last push-back
        self._queue: deque = deque()  # threads waiting for a slot, in arrival order

    def acquire(self, stop_event: threading.Event):
        """Wait for a slot, first come first served. Without the queue, the thread that just released a slot
        usually grabs it again, and at a low limit the others (e.g. a page stuck on a challenge) starve."""
        me = object()
        with self._cond:
            self._queue.append(me)
            try:
                while True:
                    if stop_event.is_set():
                        raise StopRequested()
                    wait = self._pause_until - time.time()
                    if wait <= 0 and self.active < self.limit and self._queue[0] is me:
                        self._queue.popleft()
                        self.active += 1
                        if self._queue and self.active < self.limit:
                            self._cond.notify_all()  # the next in line may go too
                        return
                    # release() wakes us; the timeout is for the end of a pause and for noticing Stop
                    self._cond.wait(timeout=min(wait, 1.0) if wait > 0 else 1.0)
            except BaseException:
                if me in self._queue:
                    self._queue.remove(me)
                self._cond.notify_all()
                raise

    def release(self, outcome: str):
        """outcome: 'ok', 'throttled' or 'other' (a normal failure, or Stop)."""
        with self._cond:
            self.active -= 1
            now = time.time()
            if outcome == "ok":
                self._blocked_since = None
                self._ok_streak += 1
                if self._ok_streak >= RAMP_EVERY:
                    self._cooldown = 0.0  # a sustained run of successes: the next push-back starts at 15 s again
                needed = RAMP_EVERY if self.limit + 1 < self._ceiling else RAMP_EVERY * 10
                if self._ok_streak >= needed and self.limit < self.max:
                    self.limit += 1
                    self._ok_streak = 0
            elif outcome == "throttled":
                if self._blocked_since is None:
                    self._blocked_since = now
                self._back_off(now, "is refusing requests (rate limit)")
            self._cond.notify_all()

    def push_back(self, reason: str):
        """Push-back that arrived as a normal-looking response (a bot-challenge page instead of data)."""
        with self._cond:
            self._back_off(time.time(), reason)
            self._cond.notify_all()

    def _back_off(self, now: float, reason: str):
        self._ok_streak = 0
        if now >= self._pause_until:  # first push-back of this wave: react once, not once per worker
            self._ceiling = self.limit
            self.limit = max(1, self.limit // 2)
            self._cooldown = min(300.0, max(15.0, self._cooldown * 2))
            self._pause_until = now + self._cooldown
            log.warning("The site %s. Pausing %.0f s, then continuing with %d concurrent requests.",
                        reason, self._cooldown, self.limit)

    def blocked_for(self) -> float:
        with self._cond:
            return time.time() - self._blocked_since if self._blocked_since else 0.0

    def state(self) -> dict:
        with self._cond:
            return {"limit": self.limit, "max": self.max, "paused_for": max(0.0, self._pause_until - time.time()),
                    "blocked_for": time.time() - self._blocked_since if self._blocked_since else 0.0}


def is_throttle_error(err: Exception) -> bool:
    text = str(err)
    return any(code in text for code in THROTTLE_CURL_CODES) or "Connection reset" in text


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
        max_active: int = START_ACTIVE,
    ):
        self.stop_event = stop_event or threading.Event()
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._local = threading.local()
        self._managers: list[FetcherSession] = []
        self._lock = threading.Lock()
        self._stealth_lock = threading.Lock()
        self.throttle = Throttle(max_active)
        self._stealth_missing = False

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

    # ---- core request with retries -----------------------------------------
    def _request(self, method: str, url: str, **kwargs):
        """One request with retries. Push-back from the site (see Throttle) doesn't use up an attempt: the shared
        cool-down paces the retry, and after GIVE_UP_AFTER of nothing but push-back the run is paused."""
        last_err = ""
        attempt = throttled = 0
        while True:
            self.check_stop()
            self.throttle.acquire(self.stop_event)
            outcome, status = "other", None
            try:
                self.sleep(random.uniform(*self.delay))
                try:
                    resp = getattr(self._session(), method)(url, **kwargs)
                except StopRequested:
                    raise
                except Exception as e:  # curl errors, timeouts, DNS…
                    last_err = f"{type(e).__name__}: {e}"
                    if is_throttle_error(e):
                        outcome = "throttled"
                else:
                    status = resp.status
                    if status not in RETRY_STATUSES:
                        outcome = "ok"
                        return resp
                    last_err = f"HTTP {status}"
                    if status in THROTTLE_STATUSES:
                        outcome = "throttled"
            finally:
                self.throttle.release(outcome)

            if outcome == "throttled":
                throttled += 1
                if self.throttle.blocked_for() > GIVE_UP_AFTER:
                    raise WafBlocked(f"the site has refused every request for {GIVE_UP_AFTER // 60} minutes "
                                     f"({last_err[:120]}). Wait an hour, then press Resume, ideally with fewer "
                                     "concurrent requests")
                if status == 403 and throttled % 2 == 0:
                    self._reset_session()  # F5 sometimes wants fresh cookies
                continue

            attempt += 1
            if attempt >= self.retries:
                break
            backoff = min(60.0, 2.0 ** attempt) + random.random()
            log.warning("%s %s failed (%s); retry %d/%d in %.0fs",
                        method.upper(), up.unquote(url)[:120], last_err[:160], attempt, self.retries - 1, backoff)
            self.sleep(backoff)
        raise FetchError(f"{method.upper()} {up.unquote(url)} failed after {self.retries} attempts: {last_err}")

    # ---- pages ----------------------------------------------------------------
    def get_page(self, path: str) -> Page | None:
        """GET a site page and return its Next.js pageProps. None for 404 / "not found" pages.

        A 200 without __NEXT_DATA__ is a bot challenge, which during a fast run means "slow down". It is handled
        like other push-back: the shared throttle halves the speed and pauses everyone, the session gets fresh
        cookies (and, every third time, a StealthyFetcher re-prime if that browser is installed), and the page is
        tried again. Only a challenge that lasts GIVE_UP_AFTER pauses the run."""
        url = site_url(path)
        first_challenge = None
        challenges = 0
        while True:
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
            challenges += 1
            first_challenge = first_challenge or time.time()
            if time.time() - first_challenge > GIVE_UP_AFTER:
                raise WafBlocked(f"the site has shown a bot challenge instead of pages for {GIVE_UP_AFTER // 60} "
                                 f"minutes (last: {up.unquote(url)[:100]}). Wait an hour, then press Resume, ideally "
                                 "with fewer concurrent requests")
            log.info("Bot challenge on %s (attempt %d); backing off", up.unquote(url)[:120], challenges)
            self.throttle.push_back("is showing bot challenges")
            if challenges % 3 == 0 and not self._stealth_missing:
                self._stealth_prime()
            else:
                self._reset_session()

    def prime(self):
        """GET the home page to (re)acquire F5 cookies and the ApiToken."""
        resp = self._request("get", BASE + "/")
        if parse.page_props(resp.body.decode("utf-8", "replace")) is None:
            self.throttle.push_back("is showing bot challenges")
            self._stealth_prime()

    def _stealth_prime(self):
        """Open the site in Scrapling's stealth browser and copy its cookies into this session. If the browser
        isn't installed (`uv run scrapling install`), remember that and fall back to fresh plain sessions."""
        with self._stealth_lock:
            if self._stealth_missing:
                self._reset_session()
                return
            log.warning("Re-priming cookies with StealthyFetcher (headless browser)…")
            try:
                from scrapling.fetchers import StealthyFetcher

                page = StealthyFetcher.fetch(BASE + "/", headless=True, network_idle=True)
            except Exception as e:
                self._stealth_missing = True
                log.warning("StealthyFetcher isn't available (%s); retrying with fresh sessions instead. "
                            "`uv run scrapling install` adds its browser.", str(e).splitlines()[0][:160])
                self._reset_session()
                return
        sess = self._reset_session()
        cookies = page.cookies or []
        items = cookies.items() if isinstance(cookies, dict) else [
            (c.get("name"), c.get("value")) for c in cookies if isinstance(c, dict)
        ]
        for name, value in items:
            if name:
                sess._curl_session.cookies.set(name, value, domain=".b144.co.il")
        if page.status and page.status >= 400:
            log.warning("StealthyFetcher got HTTP %s from the home page", page.status)

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
