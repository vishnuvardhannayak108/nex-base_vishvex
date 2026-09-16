"""Access layer: block detection precision, Camoufox budget, rate limiting."""
from __future__ import annotations

import pytest

from nexbase.access.blocking import looks_blocked, visible_text_length
from nexbase.access.fetcher import AccessLayer
from nexbase.access.ratelimit import FallbackBudget, HostRateLimiter

REAL_PAGE = (
    "<html><head><title>Careers at Acme</title></head><body>"
    + "<p>We are hiring a plant manager. " * 60
    + "</p></body></html>"
)

CLOUDFLARE_CHALLENGE = (
    "<html><head><title>Just a moment...</title></head><body>"
    "<div class='cf-chl-widget'>Checking your browser before accessing acme.com</div>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# Block detection
# ---------------------------------------------------------------------------
def test_real_page_not_flagged():
    assert looks_blocked(200, REAL_PAGE) is False


def test_genuine_challenge_flagged():
    assert looks_blocked(200, CLOUDFLARE_CHALLENGE) is True


def test_block_status_codes():
    for status in (401, 403, 429, 503):
        assert looks_blocked(status, REAL_PAGE) is True
    assert looks_blocked(200, REAL_PAGE) is False


def test_word_cloudflare_in_a_job_description_is_not_a_block():
    """The old substring matcher launched a browser for pages like this."""
    page = (
        "<html><body>"
        + "<p>Experience with Cloudflare, CDNs and DDoS mitigation preferred. " * 40
        + "</p></body></html>"
    )
    assert looks_blocked(200, page) is False


def test_noscript_enable_javascript_on_a_real_page_is_not_a_block():
    page = (
        "<html><body><noscript>Please enable JavaScript</noscript>"
        + "<p>Full job description with plenty of real content here. " * 60
        + "</p></body></html>"
    )
    assert looks_blocked(200, page) is False


def test_word_blocked_in_content_is_not_a_block():
    page = (
        "<html><body>"
        + "<p>Ensure conveyor lines are not blocked during the shift. " * 50
        + "</p></body></html>"
    )
    assert looks_blocked(200, page) is False


def test_tiny_or_empty_page_is_blocked():
    assert looks_blocked(200, "") is True
    assert looks_blocked(200, "<html><body>Access Denied</body></html>") is True


def test_visible_text_length_ignores_scripts():
    html = "<html><script>" + "x" * 5000 + "</script><body>short</body></html>"
    assert visible_text_length(html) < 100


# ---------------------------------------------------------------------------
# Budget and rate limiting
# ---------------------------------------------------------------------------
def test_fallback_budget_caps_total():
    budget = FallbackBudget(max_total=2, host_cooldown_seconds=0)
    assert budget.allows("https://a.com/1")[0] is True
    budget.consume("https://a.com/1")
    budget.consume("https://b.com/1")
    allowed, reason = budget.allows("https://c.com/1")
    assert allowed is False
    assert reason == "BUDGET_EXHAUSTED"


def test_fallback_budget_enforces_host_cooldown_after_the_allowance():
    """The cooldown starts once a host has spent its allowance, not after one page.

    A single-fallback rule abandoned 17 of 20 pages on a live company site:
    the first rescue locked the host and every later page was dropped.
    """
    budget = FallbackBudget(max_total=10, host_cooldown_seconds=300, host_max=3)
    for _ in range(3):
        assert budget.allows("https://acme.com/a")[0] is True
        budget.consume("https://acme.com/a")

    allowed, reason = budget.allows("https://acme.com/b")
    assert allowed is False
    assert reason == "HOST_COOLDOWN"
    assert budget.allows("https://other.com/a")[0] is True


def test_fallback_budget_rescues_several_pages_on_one_host():
    budget = FallbackBudget(max_total=25, host_cooldown_seconds=300, host_max=6)
    rescued = 0
    for _ in range(10):
        if budget.allows("https://acme.com/p")[0]:
            budget.consume("https://acme.com/p")
            rescued += 1
    assert rescued == 6, "a hostile host must not cost us the whole company"


def test_run_budget_still_caps_total_cost():
    budget = FallbackBudget(max_total=4, host_cooldown_seconds=0, host_max=99)
    for n in range(4):
        assert budget.allows(f"https://h{n}.com/x")[0] is True
        budget.consume(f"https://h{n}.com/x")
    assert budget.allows("https://h9.com/x") == (False, "BUDGET_EXHAUSTED")


def test_host_of():
    assert HostRateLimiter.host_of("https://www.acme.com/x") == "www.acme.com"


# ---------------------------------------------------------------------------
# Fetch flow
# ---------------------------------------------------------------------------
class FakePage:
    def __init__(self, html, status=200):
        self.html_content = html
        self.status = status


def test_scrapling_success_skips_fallback(monkeypatch, settings):
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    monkeypatch.setattr(layer, "_scrapling_get", lambda url, alternate=False: FakePage(REAL_PAGE))
    page = layer.fetch("https://acme.com/team")
    assert page.engine == "SCRAPLING"
    assert page.ok
    assert layer.fallback_count == 0


def test_alternate_attempt_before_camoufox(monkeypatch, settings):
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    calls = []

    def fake(url, alternate=False):
        calls.append(alternate)
        return FakePage(CLOUDFLARE_CHALLENGE if not alternate else REAL_PAGE)

    monkeypatch.setattr(layer, "_scrapling_get", fake)
    page = layer.fetch("https://acme.com/team")
    assert calls == [False, True]
    assert page.engine == "SCRAPLING"
    assert layer.fallback_count == 0


def test_camoufox_used_only_after_both_scrapling_attempts(monkeypatch, settings):
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    monkeypatch.setattr(
        layer, "_scrapling_get", lambda url, alternate=False: FakePage(CLOUDFLARE_CHALLENGE)
    )
    monkeypatch.setattr(
        layer,
        "_camoufox_fallback",
        lambda url: __import__("nexbase.access.fetcher", fromlist=["FetchedPage"]).FetchedPage(
            url=url, status=200, html=REAL_PAGE, title="t",
            engine="CAMOUFOX", used_fallback=True,
        ),
    )
    page = layer.fetch("https://acme.com/team")
    assert page.engine == "CAMOUFOX"
    assert page.used_fallback


def test_budget_exhaustion_returns_blocked_instead_of_launching(monkeypatch, settings):
    budget = FallbackBudget(max_total=0)
    layer = AccessLayer(settings=settings, budget=budget)
    monkeypatch.setattr(
        layer, "_scrapling_get", lambda url, alternate=False: FakePage(CLOUDFLARE_CHALLENGE)
    )

    def explode(url):
        raise AssertionError("Camoufox must not launch when the budget is spent")

    monkeypatch.setattr(layer, "_camoufox_fallback", explode)
    page = layer.fetch("https://acme.com/team")
    assert page.blocked is True
    assert page.ok is False


def test_rate_limiter_and_budget_shared_across_companies(settings):
    """One AccessLayer per run means limits do not reset per company."""
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    a, b = layer._limiter, layer._budget
    assert layer._limiter is a and layer._budget is b
    layer._budget.consume("https://x.com")
    assert layer.fallback_count == 1


def test_rendered_short_page_is_not_reported_blocked():
    """Camoufox rendered it; shortness is then real, not a JS shell."""
    tiny = "<html><head><title>Contact</title></head><body>" \
           "<p>Email us: info@acme.com</p></body></html>"
    assert looks_blocked(200, tiny) is True            # unrendered: suspicious
    assert looks_blocked(200, tiny, rendered=True) is False


def test_rendered_challenge_page_still_blocked():
    assert looks_blocked(200, CLOUDFLARE_CHALLENGE, rendered=True) is True


# ---------------------------------------------------------------------------
# Scrapling kwargs must be values the installed library actually accepts.
# Its type hint advertises follow_redirects values that raise at runtime.
# ---------------------------------------------------------------------------
_RUNTIME_SAFE_FOLLOW_REDIRECTS = (True, False, "safe")


def test_scrapling_kwargs_use_runtime_safe_values(settings):
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    for alternate in (False, True):
        kwargs = layer.scrapling_kwargs(alternate)
        assert kwargs["follow_redirects"] in _RUNTIME_SAFE_FOLLOW_REDIRECTS, (
            f"follow_redirects={kwargs['follow_redirects']!r} raises ValueError "
            "inside curl_cffi despite being in Scrapling's type hint"
        )
        assert kwargs["timeout"] == settings.request_timeout_seconds
    assert layer.scrapling_kwargs(True)["impersonate"] == "chrome"


@pytest.mark.network
def test_scrapling_kwargs_accepted_by_installed_library(settings):
    """Integration guard: both attempts must not raise against a real request."""
    from scrapling import Fetcher

    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    for alternate in (False, True):
        response = Fetcher.get("https://example.com", **layer.scrapling_kwargs(alternate))
        assert response.status == 200


def test_404_is_not_escalated_to_camoufox(monkeypatch, settings):
    """A missing page is an answer; retrying cannot conjure it."""
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    attempts = []

    def fake(url, alternate=False):
        attempts.append(alternate)
        return FakePage("", status=404)

    monkeypatch.setattr(layer, "_scrapling_get", fake)
    monkeypatch.setattr(
        layer, "_camoufox_fallback",
        lambda url: (_ for _ in ()).throw(AssertionError("must not launch for a 404")),
    )
    page = layer.fetch("https://acme.com/leadership")
    assert attempts == [False], "only one attempt for a 404"
    assert page.error == "NOT_FOUND"
    assert page.ok is False
    assert layer.fallback_count == 0


def test_403_still_escalates(monkeypatch, settings):
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    monkeypatch.setattr(
        layer, "_scrapling_get", lambda url, alternate=False: FakePage("", status=403)
    )
    launched = []
    monkeypatch.setattr(
        layer, "_camoufox_fallback",
        lambda url: launched.append(url) or __import__(
            "nexbase.access.fetcher", fromlist=["FetchedPage"]
        ).FetchedPage(url, 200, REAL_PAGE, "t", "CAMOUFOX", True),
    )
    layer.fetch("https://acme.com/team")
    assert launched, "a 403 is a genuine block and must escalate"


# ---------------------------------------------------------------------------
# Host circuit breaker
# ---------------------------------------------------------------------------
def test_circuit_breaker_stops_hammering_a_dead_host(monkeypatch, settings):
    """A site that blackholes connections must not consume 11 timeouts."""
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    attempts = []

    def always_timeout(url, alternate=False):
        attempts.append(url)
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(layer, "_scrapling_get", always_timeout)
    monkeypatch.setattr(
        layer, "_camoufox_fallback",
        lambda url: (_ for _ in ()).throw(TimeoutError("nav timeout")),
    )

    for path in ("/leadership", "/team", "/about", "/company", "/staff", "/contact"):
        layer.fetch(f"https://dead.example{path}")

    # Threshold is 3; once open, no further transport attempts are made.
    assert len(attempts) < 12, f"kept hammering: {len(attempts)} attempts"
    last = layer.fetch("https://dead.example/anything")
    assert last.error == "HOST_CIRCUIT_OPEN"
    assert last.engine == "NONE"


def test_circuit_breaker_resets_on_success(settings):
    from nexbase.access.ratelimit import HostCircuitBreaker

    breaker = HostCircuitBreaker(failure_threshold=2)
    breaker.record_failure("https://acme.com/a")
    assert breaker.is_open("https://acme.com/b") is False
    breaker.record_failure("https://acme.com/b")
    assert breaker.is_open("https://acme.com/c") is True
    breaker.record_success("https://acme.com/c")
    assert breaker.is_open("https://acme.com/d") is False


def test_404_keeps_host_healthy(monkeypatch, settings):
    """A 404 proves the host is up; only the path is missing."""
    layer = AccessLayer(settings=settings)
    layer.respect_robots = False
    layer._url_resolver = lambda host: ["93.184.216.34"]  # skip DNS, not the guard
    monkeypatch.setattr(
        layer, "_scrapling_get", lambda url, alternate=False: FakePage("", status=404)
    )
    for path in ("/leadership", "/team", "/about", "/company", "/staff"):
        layer.fetch(f"https://alive.example{path}")
    assert layer._breaker.is_open("https://alive.example/x") is False
