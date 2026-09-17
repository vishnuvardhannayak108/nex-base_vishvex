"""The Access Layer.

Implements the non-negotiable fetch order for every page request:

1. Scrapling first.
2. If blocked -> retry / alternate method (still Scrapling).
3. If still blocked -> Camoufox for that single page only.
4. Immediately switch back to Scrapling for all subsequent pages.

Camoufox is an expensive, temporary fallback and is never the default. It is
budgeted per run and rate-limited per host; when the budget is exhausted the
layer returns the best blocked response it has rather than launching a browser.

One :class:`AccessLayer` should be shared for a whole pipeline run so the rate
limiter and the fallback budget are actually global.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from nexbase.access.blocking import (
    NOT_FOUND_STATUS_CODES,
    RATE_LIMIT_STATUS_CODES,
    looks_blocked,
)
from nexbase.access.urlguard import (
    HostUnresolved,
    UrlRejected,
    check_url,
    resolve_host,
)
from nexbase.access.ratelimit import (
    FallbackBudget,
    HostCircuitBreaker,
    HostRateLimiter,
)
from nexbase.config import Settings, get_settings
from nexbase.logging_setup import get_logger

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class FetchedPage:
    """A single fetched page with metadata about how it was obtained."""

    url: str
    status: int | None
    html: str
    title: str
    engine: str  # "SCRAPLING" | "CAMOUFOX" | "NONE"
    used_fallback: bool
    blocked: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Usable content: a body, not blocked, and not an HTTP error page.

        A 409 Cloudflare "DNS resolution error" page or a 500 page has a body
        but says nothing about the site; reading it as content is wrong.
        """
        ok_status = self.status is None or self.status < 400
        return bool(self.html) and not self.blocked and ok_status


class AccessLayer:
    """Orchestrates page fetching across Scrapling and Camoufox."""

    def __init__(
        self,
        settings: Settings | None = None,
        logger=None,
        rate_limiter: HostRateLimiter | None = None,
        budget: FallbackBudget | None = None,
        breaker: HostCircuitBreaker | None = None,
        repo=None,
        url_resolver=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.access")
        self._limiter = rate_limiter or HostRateLimiter(
            self.settings.rate_limit_per_second,
            per_host_rate_per_second=max(0.2, self.settings.rate_limit_per_second / 4),
        )
        self._budget = budget or FallbackBudget(
            max_total=self.settings.camoufox_budget_per_run,
            host_cooldown_seconds=self.settings.camoufox_domain_cooldown_seconds,
            host_max=self.settings.camoufox_host_max_fallbacks,
        )
        self._breaker = breaker or HostCircuitBreaker(
            failure_threshold=self.settings.host_failure_threshold
        )
        self.repo = repo
        #: Injectable so the SSRF guard can be exercised without DNS.
        self._url_resolver = url_resolver or resolve_host
        self.respect_robots = (
            settings_robots
            if (settings_robots := getattr(self.settings, "respect_robots", True))
            is not None else True
        )
        self._robots: dict[str, object] = {}
        self.robots_blocked = 0
        self.ssrf_blocked = 0
        self.unresolved_hosts = 0
        self.rate_limited = 0
        self.pages_fetched = 0
        self.pages_blocked = 0

    @property
    def fallback_count(self) -> int:
        return self._budget.used

    # ------------------------------------------------------------------
    # robots.txt
    # ------------------------------------------------------------------
    def robots_allows(self, url: str) -> bool:
        """Consult the host's robots.txt for this URL.

        This lives on the access layer rather than on one caller because every
        self-driven fetch in the system goes through :meth:`fetch`. Previously
        only the optional size-resolution scanner checked robots, which meant
        the default configuration checked nothing at all.
        """
        if not self.respect_robots:
            return True
        from urllib.parse import urlsplit
        from urllib.robotparser import RobotFileParser

        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return True
        base = f"{parts.scheme}://{parts.netloc}"
        if parts.path.rstrip("/") == "/robots.txt" or parts.path == "/robots.txt":
            return True
        if base not in self._robots:
            self._robots[base] = self._load_robots(base)
        parser = self._robots[base]
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.settings.user_agent_token, url)
        except Exception:
            return True

    def _load_robots(self, base: str):
        """Fetch robots.txt with a single plain attempt.

        Deliberately not the full ladder: robots.txt is plain text, so the
        block-detection heuristics misread it, and escalating to Camoufox for a
        policy file would spend the run's fallback budget on metadata. A host
        that will not serve robots.txt is treated as having none.
        """
        from urllib.robotparser import RobotFileParser

        page, _ = self._try_scrapling(f"{base}/robots.txt", alternate=False)
        if page is None:
            return None
        body = str(getattr(page, "html_content", "") or "")
        if not body or getattr(page, "status", None) in NOT_FOUND_STATUS_CODES:
            return None
        page = FetchedPage(f"{base}/robots.txt", getattr(page, "status", None),
                           body, "", "SCRAPLING", False)
        parser = RobotFileParser()
        try:
            parser.parse(page.html.splitlines())
        except Exception:
            return None
        return parser

    # ------------------------------------------------------------------
    def fetch(self, url: str) -> FetchedPage:
        """Fetch a page following the locked access-layer rules.

        Two gates run before any request leaves the process: the URL must be a
        public http(s) address (SSRF), and robots.txt must permit it.
        """
        try:
            check_url(url, resolver=self._url_resolver)
        except HostUnresolved as exc:
            # A dead domain, not an attack. Counted separately so the blocked
            # total reflects hosts that actually refused us.
            self.unresolved_hosts += 1
            self.log.info("host_unresolved", url=url, reason=str(exc))
            return FetchedPage(
                url=url, status=None, html="", title="", engine="NONE",
                used_fallback=False, blocked=True, error=f"HOST_UNRESOLVED:{exc}",
            )
        except UrlRejected as exc:
            self.ssrf_blocked += 1
            self.log.warning("url_rejected", url=url, reason=str(exc))
            return FetchedPage(
                url=url, status=None, html="", title="", engine="NONE",
                used_fallback=False, blocked=True, error=f"URL_REJECTED:{exc}",
            )

        if not self.robots_allows(url):
            self.robots_blocked += 1
            self.log.info("robots_disallowed", url=url)
            return FetchedPage(
                url=url, status=None, html="", title="", engine="NONE",
                used_fallback=False, blocked=True, error="ROBOTS_DISALLOWED",
            )
        return self._raw_fetch(url)

    def _raw_fetch(self, url: str) -> FetchedPage:
        """The fetch ladder itself, with the pre-flight gates already applied."""
        # A host that has already failed repeatedly is skipped outright; each
        # further attempt would cost a full timeout for no plausible gain.
        if self._breaker.is_open(url):
            self.log.info(
                "host_circuit_open", url=url, failures=self._breaker.failures(url)
            )
            return FetchedPage(
                url=url, status=None, html="", title="", engine="NONE",
                used_fallback=False, blocked=True, error="HOST_CIRCUIT_OPEN",
            )

        self._limiter.wait(url)

        # 1. Scrapling first.
        page, error = self._try_scrapling(url, alternate=False)

        # A 404/410 is an answer, not a block. Escalating cannot help. Checked
        # before success: a site's own 404 page has a full body, so it passed
        # the block check and came back as a readable page.
        if page is not None and page.status in NOT_FOUND_STATUS_CODES:
            # The host answered, so it is healthy - only the path is missing.
            self._breaker.record_success(url)
            self.log.debug("page_not_found", url=url, status=page.status)
            return FetchedPage(
                url=url, status=page.status, html="", title="",
                engine="SCRAPLING", used_fallback=False,
                blocked=True, error="NOT_FOUND",
            )

        if page is not None and not looks_blocked(page.status, page.html_content):
            self._breaker.record_success(url)
            return self._build_page(url, page, "SCRAPLING", used_fallback=False)

        if page is None:
            # A transport failure (timeout, DNS, refused connection).
            self._breaker.record_failure(url)

        # 2. Retry / alternate method, still Scrapling.
        self.log.warning(
            "scrapling_blocked", url=url, status=getattr(page, "status", None), error=error
        )
        self._limiter.wait(url)
        alt, alt_error = self._try_scrapling(url, alternate=True)
        if alt is not None and not looks_blocked(alt.status, alt.html_content):
            self._breaker.record_success(url)
            return self._build_page(url, alt, "SCRAPLING", used_fallback=False)
        if alt is None:
            self._breaker.record_failure(url)

        last = alt if alt is not None else page
        last_error = alt_error or error

        # A rate limit is not a bot wall: launching a browser cannot satisfy
        # "you are asking too often", it just spends the fallback budget. A
        # live benchmark burned 5-7 launches per run on Brave 429s for nothing.
        status = getattr(last, "status", None)
        if status in RATE_LIMIT_STATUS_CODES:
            self.rate_limited += 1
            self.log.info("rate_limited", url=url, status=status)
            return self._blocked_page(url, last, f"RATE_LIMITED:{status}")

        # 3. Camoufox for this single page only, then back to Scrapling.
        allowed, deny_reason = self._budget.allows(url)
        if not allowed:
            self.log.warning("camoufox_fallback_skipped", url=url, reason=deny_reason)
            self._audit("CAMOUFOX_FALLBACK_SKIPPED", url, deny_reason)
            return self._blocked_page(url, last, last_error or deny_reason)

        try:
            rendered = self._camoufox_fallback(url)
            self._breaker.record_success(url)
            return rendered
        except Exception as exc:
            self._breaker.record_failure(url)
            self.log.error("camoufox_fallback_error", url=url, error=str(exc))
            return self._blocked_page(url, last, str(exc))

    # ------------------------------------------------------------------
    # Scrapling
    # ------------------------------------------------------------------
    def _try_scrapling(self, url: str, alternate: bool):
        try:
            return self._scrapling_get(url, alternate=alternate), None
        except Exception as exc:
            event = "scrapling_alt_fetch_error" if alternate else "scrapling_fetch_error"
            self.log.warning(event, url=url, error=str(exc))
            return None, str(exc)

    def _scrapling_get(self, url: str, alternate: bool = False):
        from scrapling import Fetcher

        return Fetcher.get(url, **self.scrapling_kwargs(alternate))

    def scrapling_kwargs(self, alternate: bool) -> dict:
        """Build Scrapling kwargs for a fetch attempt.

        ``follow_redirects`` accepts only ``True``/``False``/``"safe"`` at
        runtime in scrapling 0.4.15. Its type hint also advertises ``"all"``,
        ``"obeycode"`` and ``"firstonly"``, but those raise
        ``ValueError: invalid literal for int()`` inside curl_cffi. Passing one
        made every alternate attempt fail, so every blocked page skipped
        straight to Camoufox.
        """
        kwargs: dict = {
            "timeout": self.settings.request_timeout_seconds,
            "retries": self.settings.max_retries,
            "stealthy_headers": True,
        }
        if alternate:
            # Second attempt: full browser impersonation, follow every redirect.
            kwargs["impersonate"] = "chrome"
            kwargs["follow_redirects"] = True
        else:
            kwargs["follow_redirects"] = "safe"
        return kwargs

    # ------------------------------------------------------------------
    def final_url_allowed(self, page) -> str | None:
        """Re-check where a redirect actually landed.

        The pre-flight guard validates the URL we asked for. Redirects are
        followed inside the HTTP client, so a public host can still hand us
        127.0.0.1 or the metadata endpoint on the second hop. Returns the
        rejection reason, or None when the destination is acceptable.
        """
        final = getattr(page, "url", None)
        if not final:
            return None
        try:
            check_url(str(final), resolver=self._url_resolver)
        except UrlRejected as exc:
            return str(exc)
        return None

    def _build_page(self, url: str, page, engine: str, used_fallback: bool) -> FetchedPage:
        reason = self.final_url_allowed(page)
        if reason is not None:
            self.ssrf_blocked += 1
            self.log.warning("redirect_rejected", url=url,
                             final=str(getattr(page, "url", "")), reason=reason)
            return FetchedPage(
                url=url, status=getattr(page, "status", None), html="", title="",
                engine=engine, used_fallback=used_fallback, blocked=True,
                error=f"REDIRECT_REJECTED:{reason}",
            )
        self.pages_fetched += 1
        html = str(page.html_content or "")
        match = _TITLE_RE.search(html)
        title = match.group(1).strip() if match else ""
        self.log.info(
            "page_fetched",
            url=url,
            engine=engine,
            status=getattr(page, "status", None),
            bytes_len=len(html),
        )
        return FetchedPage(
            url=url,
            status=getattr(page, "status", None),
            html=html,
            title=title,
            engine=engine,
            used_fallback=used_fallback,
        )

    def _blocked_page(self, url: str, page, error: str | None) -> FetchedPage:
        self.pages_blocked += 1
        html = str(getattr(page, "html_content", "") or "") if page is not None else ""
        return FetchedPage(
            url=url,
            status=getattr(page, "status", None) if page is not None else None,
            html=html,
            title="",
            engine="SCRAPLING" if page is not None else "NONE",
            used_fallback=False,
            blocked=True,
            error=error,
        )

    def _audit(self, event: str, url: str, detail: str | None = None) -> None:
        if self.repo is None:
            return
        try:
            self.repo.insert_audit(
                {"event": event, "message": url, "data": {"detail": detail}, "level": "WARNING"}
            )
        except Exception:  # auditing must never break a fetch
            pass

    # ------------------------------------------------------------------
    # Camoufox (single-page fallback)
    # ------------------------------------------------------------------
    def _camoufox_fallback(self, url: str) -> FetchedPage:
        from camoufox.sync_api import Camoufox

        self.log.warning("camoufox_fallback_triggered", url=url, used=self._budget.used)
        self._budget.consume(url)
        self._audit("CAMOUFOX_FALLBACK", url)

        timeout_ms = self.settings.request_timeout_seconds * 1000
        settle_ms = max(0, int(self.settings.camoufox_settle_seconds * 1000))
        with Camoufox(headless=True) as browser:
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                # A bounded settle only. The reason to reach for Camoufox at all
                # is content that arrives after first paint, but an unbounded
                # wait would let one slow page consume the run, so this is a
                # short fixed ceiling rather than "wait until idle".
                if settle_ms:
                    try:
                        page.wait_for_load_state("networkidle", timeout=settle_ms)
                    except Exception:
                        page.wait_for_timeout(settle_ms)
                html = page.content()
                title = page.title()
                final_reason = self.final_url_allowed(page)
            finally:
                page.close()

        self.log.info("camoufox_fallback_complete", url=url, bytes_len=len(html))
        if final_reason is not None:
            self.ssrf_blocked += 1
            self.log.warning("redirect_rejected", url=url, reason=final_reason)
            return FetchedPage(
                url=url, status=None, html="", title="", engine="CAMOUFOX",
                used_fallback=True, blocked=True,
                error=f"REDIRECT_REJECTED:{final_reason}",
            )
        return FetchedPage(
            url=url,
            status=None,
            html=html,
            title=title,
            engine="CAMOUFOX",
            used_fallback=True,
            blocked=looks_blocked(None, html, rendered=True),
        )


async def afetch(url: str, layer: AccessLayer | None = None) -> FetchedPage:
    """Async wrapper around the sync access layer (for FastAPI)."""
    import asyncio

    layer = layer or AccessLayer()
    return await asyncio.to_thread(layer.fetch, url)
