"""Heuristics for detecting bot-blocked pages.

Precision matters more than recall here: a false positive costs a Camoufox
launch (seconds of CPU and a real browser process), so markers are matched
against the *head* of the document and against structural signatures rather
than as bare substrings anywhere in the body. A careers page that merely
mentions "Cloudflare" in a job description must not trigger a fallback.
"""
from __future__ import annotations

import re

BLOCK_STATUS_CODES = frozenset({401, 403, 407, 429, 503})

#: Blocked, but by a rate limiter rather than a bot wall. Rendering the
#: page in a browser cannot satisfy "you are asking too often", so these
#: must not escalate to Camoufox - a live benchmark burned 5-7 browser
#: launches per run on Brave 429s and resolved nothing extra.
RATE_LIMIT_STATUS_CODES = frozenset({429, 503})

#: A definitive "this page does not exist" answer. Not a block: retrying with
#: another engine cannot conjure the page, so escalation must stop here.
#: Contact discovery probes ~11 speculative paths per company, most of which
#: 404; treating those as blocked cost two extra attempts and a browser launch
#: each.
NOT_FOUND_STATUS_CODES = frozenset({404, 410})

#: Signatures that only appear on genuine interstitial/challenge pages.
#: Matched case-insensitively against the first `HEAD_WINDOW` characters.
_STRONG_MARKERS = (
    "cf-chl-",
    "cf_chl_opt",
    "__cf_chl",
    "/cdn-cgi/challenge-platform",
    "just a moment...",
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "ddos-guard",
    "please verify you are a human",
    "are you a robot",
    "unusual traffic from your computer network",
    "access to this page has been denied",
    "px-captcha",
    "perimeterx",
    "incapsula incident id",
    "request unsuccessful. incapsula",
    "captcha-delivery.com",
    "g-recaptcha",
    "h-captcha",
    "hcaptcha.com/captcha",
    # Search-engine anti-bot interstitials (seen live 2026-09-14, HTTP 202).
    "bots use duckduckgo",
    "complete the following challenge",
    "confirm this search was made by a human",
    "select all squares containing",
    "our systems have detected unusual traffic",
)

#: Weaker signals: only meaningful when the document is also tiny, i.e. the
#: page has no real content behind them.
_WEAK_MARKERS = (
    "access denied",
    "403 forbidden",
    "blocked",
    "enable javascript",
    "javascript is required",
    "captcha",
)

#: Root elements of non-HTML documents fetched as pages (sitemaps).
_DOCUMENT_ROOTS = ("<?xml", "<urlset", "<sitemapindex")

#: Only the first N characters are inspected for markers.
HEAD_WINDOW = 4000

#: A body shorter than this carries no useful content.
MIN_MEANINGFUL_HTML_LENGTH = 500

#: A weak marker only counts on a page that is *also* nearly contentless.
#: A real job page mentioning "blocked" or carrying a <noscript> notice has
#: far more text than this, and must not trigger an expensive fallback.
WEAK_MARKER_MAX_LENGTH = 1200

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)


def visible_text_length(html: str) -> int:
    """Approximate length of the human-visible text in a document."""
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    return len(_TAG_RE.sub(" ", stripped).strip())


def looks_blocked(
    status: int | None, html: str | None, rendered: bool = False
) -> bool:
    """Return True if a status code or page body indicates anti-bot blocking.

    ``rendered=True`` means the HTML came from a real browser (Camoufox). A
    short page is then genuinely short rather than an unexecuted JS shell, so
    the size heuristics are skipped and only positive block evidence counts.
    Without this, a small-but-valid page fetched as a last resort was still
    reported blocked and its content thrown away.
    """
    if status is not None and status in BLOCK_STATUS_CODES:
        return True

    text = html or ""
    if not text.strip():
        return True

    head = text[:HEAD_WINDOW].lower()
    if any(marker in head for marker in _STRONG_MARKERS):
        return True

    # A challenge page announces itself in the <title>; a job page that merely
    # mentions "blocked" or "captcha" in its body does not.
    title_match = _TITLE_RE.search(text[:HEAD_WINDOW])
    if title_match:
        title = title_match.group(1).lower()
        if any(marker in title for marker in _WEAK_MARKERS):
            return True

    if rendered:
        # A real browser already executed the page; shortness is not evidence.
        return False

    # A sitemap index or a JSON response is short by nature; the visible-text
    # heuristics below are for HTML and marked every small sitemap blocked.
    # Scrapling serves XML wrapped as "<html><body><sitemapindex ...", so the
    # root element is looked for near the start rather than at it.
    start = text.lstrip()[:200].lower()
    if start[:1] in ("{", "[") or any(root in start for root in _DOCUMENT_ROOTS):
        return False

    body_len = visible_text_length(text)
    if body_len < MIN_MEANINGFUL_HTML_LENGTH:
        return True

    # Weak markers are decisive only on an otherwise contentless page.
    if body_len < WEAK_MARKER_MAX_LENGTH and any(m in head for m in _WEAK_MARKERS):
        return True

    return False
