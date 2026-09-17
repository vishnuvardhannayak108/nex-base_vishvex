"""Title-to-priority inference and contact extraction.

Uses structured selectors (JSON-LD, h-card/vcard, staff tables, LinkedIn and
mailto links). On employer-site people pages only (the caller decides), explicit
plain-text name/title pairs are read too: "Andy Dupuy - CEO & President",
"Fred McManus, COO", or a name line followed by a title line. Both halves must
pass: a plausible person name and a title on the POC list. Nothing is inferred
from prose and no email address is ever constructed from a pattern.

The POC priority tiers are exactly the Master Plan's:
  P1 Owner / CEO / President / Managing Partner
  P2 COO / VP Operations / Director of Operations / General Manager
  P3 HR Director / HR Manager / Head of HR / People / Talent Acquisition
  P4 Plant Manager / Operations Manager
Each tier lists those titles and their spelled-out forms only. Titles are
matched as whole words on a normalized title, so "HR Coordinator" is not a COO
and "Vice President of Sales" is not a President.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote

from bs4 import BeautifulSoup

from nexbase.contacts.models import ContactCandidate

#: Written in normalized form: lowercase, punctuation as spaces, "&" as "and",
#: and "vice president" / "svp" / "evp" as "vp".
P1_TITLES = (
    "owner", "co owner", "ceo", "chief executive", "chief executive officer",
    "president", "managing partner",
)
P2_TITLES = (
    "coo", "chief operating officer", "vp operations", "vp of operations",
    "director of operations", "director operations", "operations director",
    "general manager",
)
P3_TITLES = (
    "hr director", "director of hr", "hr manager", "manager of hr", "head of hr",
    "human resources director", "director of human resources",
    "human resources manager", "manager of human resources", "head of human resources",
    "head of people", "people director", "director of people", "vp people",
    "vp of people", "chief people officer", "talent acquisition",
)
P4_TITLES = ("plant manager", "operations manager")

#: Whole words or phrases that make a title something other than the listed
#: role: "Assistant Plant Manager", "Former CEO", "Owner Operator" (a truck
#: driver), "Product Owner" (a software role).
_TITLE_EXCLUSIONS = (
    "assistant", "associate", "deputy", "former", "future", "aspiring",
    "owner operator", "product owner", "process owner",
)


#: A president of a division, region or group is not the company President:
#: "President of Maintenance", "Division President". Removed before the P1 test.
_QUALIFIED_PRESIDENT = re.compile(
    r"\b(?:(?:division|divisional|regional|region|area|group|district|segment|"
    r"sector|business|unit|branch|market|zone) president|president (?:of|for)\b.*$)"
)


def _normalize_title(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", title.lower().replace("&", " and "))
    # "Senior/Executive Vice President" (SVP, EVP) is a vice president too.
    return re.sub(r"\b(?:vice president|svp|evp)\b", "vp", _WS.sub(" ", text).strip())


def _words_pattern(phrases) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b")


_PRIORITY_PATTERNS = {
    tier: _words_pattern(titles)
    for tier, titles in ((1, P1_TITLES), (2, P2_TITLES), (3, P3_TITLES), (4, P4_TITLES))
}
_EXCLUSION_PATTERN = _words_pattern(_TITLE_EXCLUSIONS)

_MAILTO_RE = re.compile(r"^mailto:([^?]+)", re.IGNORECASE)
#: "jane [at] acme [dot] com" and friends.
#:
#: The separators MUST be bracketed. An earlier version accepted a bare " at "
#: and a bare "." too, which matched ordinary prose: a live run turned
#: "celebration. Learn" into celebr@ion.learn and "patients. Read" into
#: p@ients.read. Inventing an address is worse than missing one, so only the
#: unambiguous bracketed form is decoded.
_OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[({<]\s*(?:at|@)\s*[\])}>]\s*"
    r"([A-Za-z0-9.\-]+?)\s*[\[({<]\s*(?:dot|\.)\s*[\])}>]\s*([A-Za-z]{2,24})",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_WS = re.compile(r"\s+")

#: Role/shared mailboxes are not people.
_ROLE_LOCALPARTS = frozenset(
    {
        "info", "contact", "hello", "support", "sales", "admin", "office",
        "help", "team", "careers", "jobs", "hr", "recruiting", "noreply",
        "no-reply", "donotreply", "webmaster", "postmaster", "marketing",
        "billing", "accounts", "enquiries", "inquiries", "general",
        # HR/recruiting mailboxes seen on real employer sites. A live contact
        # run returned hrservicecenter@lowes.com and classified it as a person.
        "hrservicecenter", "humanresources", "human-resources", "talent",
        "talentacquisition", "recruiter", "recruiters", "recruitment",
        "staffing", "hiring", "employment", "apply", "applications",
        "resume", "resumes", "cv", "hrdept", "hrdepartment", "peopleops",
        # Department mailboxes small employers publish instead of a person.
        "orders", "order", "service", "customerservice", "customer-service",
        "estimating", "estimates", "quotes", "quote", "dispatch", "accounting",
        "payroll", "purchasing", "shipping", "receiving", "reception",
        "frontdesk", "safety", "operations", "ops", "mail", "email", "feedback",
        "press", "media", "privacy", "legal", "compliance", "security",
    }
)

#: Substrings that make a local part a shared mailbox even when the exact
#: spelling is not listed, e.g. "hr-service-center", "careers.us".
_ROLE_TOKENS = (
    "recruit", "staffing", "hiring", "career", "talent", "humanresource",
    "human-resource", "hrservice", "hr-service", "employment", "jobs",
)


def is_role_email(email: str | None) -> bool:
    """True when the address is a shared/role mailbox rather than a person."""
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].strip().lower()
    if local in _ROLE_LOCALPARTS:
        return True
    return any(token in local for token in _ROLE_TOKENS)


def infer_priority(title: str | None) -> int | None:
    """Map a title to its POC priority tier (1..4), or None.

    The most senior tier named wins: "President & COO" is P1.
    """
    if not title:
        return None
    normalized = _normalize_title(title)
    if _EXCLUSION_PATTERN.search(normalized):
        return None
    for tier, pattern in _PRIORITY_PATTERNS.items():
        text = _QUALIFIED_PRESIDENT.sub(" ", normalized) if tier == 1 else normalized
        if pattern.search(text):
            return tier
    return None


@dataclass
class ExtractedContact:
    name: str | None
    title: str | None
    email: str | None
    profile_url: str | None
    method: str


def _clean_text(value: str | None, limit: int = 160) -> str | None:
    if not value:
        return None
    cleaned = _WS.sub(" ", value).strip("  -–—,·|")
    return cleaned[:limit] or None


#: Words that appear in link text but never in a person's name. A live run on
#: cardinalhealth.com stored "text message" three times and "email us" once as
#: named contacts, because the mailto label was taken as a name.
_NOT_NAME_WORDS = frozenset(
    {
        "email", "e-mail", "mail", "us", "here", "click", "contact", "call",
        "text", "message", "send", "reply", "apply", "now", "more", "info",
        "support", "help", "team", "sales", "careers", "jobs", "hr", "form",
        "learn", "read", "view", "download", "subscribe", "login", "sign",
        "chat", "today", "free", "get", "start", "request", "submit", "phone",
    }
)


def _valid_person_name(name: str | None) -> bool:
    """Reject obvious non-names without being clever about it."""
    if not name:
        return False
    cleaned = name.strip()
    if len(cleaned) < 3 or len(cleaned) > 80:
        return False
    if "@" in cleaned or cleaned.startswith("http"):
        return False
    words = cleaned.split()
    if not 1 < len(words) <= 5:
        return False
    if not any(ch.isalpha() for ch in cleaned):
        return False
    # A call to action is not a person. Both tests have to pass: real names
    # capitalise ("Julia Zhen"), and none of their words are UI vocabulary.
    if any(w.strip(".,").lower() in _NOT_NAME_WORDS for w in words):
        return False
    return sum(1 for w in words if w[:1].isupper()) >= 2


def _jsonld_contacts(soup: BeautifulSoup) -> list[ExtractedContact]:
    contacts: list[ExtractedContact] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            if isinstance(node.get("@graph"), list):
                stack.extend(node["@graph"])
            for key in ("employee", "employees", "founder", "member"):
                value = node.get(key)
                if isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, dict):
                    stack.append(value)
            atype = node.get("@type")
            types = atype if isinstance(atype, list) else [atype]
            if not any(str(t).lower() == "person" for t in types if t):
                continue
            url = node.get("url") or node.get("sameAs")
            if isinstance(url, list):
                url = url[0] if url else None
            contacts.append(
                ExtractedContact(
                    name=_clean_text(node.get("name")),
                    title=_clean_text(node.get("jobTitle")),
                    email=(node.get("email") or "").replace("mailto:", "").strip() or None,
                    profile_url=url if isinstance(url, str) else None,
                    method="jsonld",
                )
            )
    return contacts


def _microformat_contacts(soup: BeautifulSoup) -> list[ExtractedContact]:
    contacts: list[ExtractedContact] = []
    for card in soup.select(".h-card, .vcard, [class*='h-card'], [itemtype*='schema.org/Person']"):
        name_el = (
            card.select_one(".p-name, .fn, [itemprop='name']")
            or card.select_one("[class*='name']")
        )
        title_el = (
            card.select_one(".p-job-title, .job-title, [itemprop='jobTitle']")
            or card.select_one("[class*='title'], [class*='role'], [class*='position']")
        )
        email_el = card.select_one(".u-email, [itemprop='email'], a[href^='mailto:']")

        email = None
        if email_el is not None:
            href = email_el.get("href") or ""
            match = _MAILTO_RE.match(href)
            email = match.group(1).strip() if match else _clean_text(email_el.get_text())

        contact = ExtractedContact(
            name=_clean_text(name_el.get_text() if name_el else None),
            title=_clean_text(title_el.get_text() if title_el else None),
            email=email,
            profile_url=None,
            method="microformat",
        )
        if contact.name or contact.title:
            contacts.append(contact)
    return contacts


def _looks_like_person_name(text: str | None) -> bool:
    """Heuristic for 'this cell holds a human name'."""
    if not text:
        return False
    cleaned = _WS.sub(" ", text).strip(" .,-")
    if not 3 <= len(cleaned) <= 60 or "@" in cleaned or cleaned.startswith("http"):
        return False
    if any(ch.isdigit() for ch in cleaned):
        return False
    words = [w for w in cleaned.split() if w]
    if not 2 <= len(words) <= 4:
        return False
    if infer_priority(cleaned) is not None:
        return False  # it is a job title, not a name
    alpha = sum(ch.isalpha() or ch in "-'." for ch in cleaned)
    return alpha / max(len(cleaned), 1) > 0.8


def _table_contacts(soup: BeautifulSoup) -> list[ExtractedContact]:
    """Extract staff directories laid out as HTML tables.

    Small and mid-size employers overwhelmingly publish their leadership as a
    plain table - ``| Cynthia Slezak | CEO | 136 | 234-200-0780 |
    cslezak@acme.com |``. Reading only the innermost cell around a ``mailto:``
    yields the address with no person attached, so the whole row is used and
    the name/title cells recovered from the same record.
    """
    contacts: list[ExtractedContact] = []
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue

        texts = [_WS.sub(" ", c.get_text(" ", strip=True)).strip() for c in cells]

        email = None
        link = row.select_one("a[href^='mailto:']")
        if link is not None:
            match = _MAILTO_RE.match(link.get("href") or "")
            if match:
                email = match.group(1).strip()
        if email is None:
            for text in texts:
                found = _EMAIL_RE.search(text)
                if found:
                    email = found.group(0)
                    break
        if not email:
            continue

        name = next((t for t in texts if _looks_like_person_name(t)), None)
        title = next((t for t in texts if infer_priority(t) is not None), None)
        if title is None and name is not None:
            # Fall back to the cell immediately after the name.
            try:
                idx = texts.index(name)
                candidate = texts[idx + 1] if idx + 1 < len(texts) else None
            except ValueError:
                candidate = None
            if candidate and "@" not in candidate and not candidate.isdigit():
                title = candidate

        contacts.append(
            ExtractedContact(
                name=name,
                title=title,
                email=email if not is_role_email(email) else None,
                profile_url=None,
                method="table",
            )
        )
    return contacts


def _block_contacts(soup: BeautifulSoup) -> list[ExtractedContact]:
    """Extract stacked card layouts: name, title and email in one block."""
    contacts: list[ExtractedContact] = []
    seen_blocks: set[int] = set()

    for link in soup.select("a[href^='mailto:']"):
        match = _MAILTO_RE.match(link.get("href") or "")
        if not match:
            continue
        email = match.group(1).strip()

        # Walk outward until an ancestor carries more than just the address.
        node = link
        block = None
        for _ in range(4):
            node = node.parent
            if node is None or node.name in ("body", "html"):
                break
            text = _WS.sub(" ", node.get_text(" ", strip=True))
            if len(text.replace(email, "").strip()) >= 6:
                block = node
                break
        if block is None or id(block) in seen_blocks:
            continue
        seen_blocks.add(id(block))

        lines = [
            _WS.sub(" ", part).strip(" |·-–—,")
            for part in block.get_text("\n", strip=True).split("\n")
        ]
        lines = [ln for ln in lines if ln and "@" not in ln]

        name = next((ln for ln in lines if _looks_like_person_name(ln)), None)
        title = next((ln for ln in lines if infer_priority(ln) is not None), None)

        contacts.append(
            ExtractedContact(
                name=name,
                title=title,
                email=email if not is_role_email(email) else None,
                profile_url=None,
                method="block",
            )
        )
    return contacts


def extract_emails_from_html(html: str | None) -> list[str]:
    """Every publicly published address on a page, observed never guessed.

    Used for company-level contactability when an address cannot be attributed
    to a named person. Role mailboxes are included here (the caller separates
    them); obviously non-human addresses are not.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, None] = {}

    for link in soup.select("a[href^='mailto:']"):
        match = _MAILTO_RE.match(link.get("href") or "")
        if match:
            address = unquote(match.group(1)).strip().lower()
            if _EMAIL_RE.fullmatch(address):
                found.setdefault(address, None)

    # Addresses published in an attribute rather than as text. These are still
    # explicit - the page states the address - so reading them is not guessing.
    for node in soup.select("[data-email], [data-mail]"):
        raw = (node.get("data-email") or node.get("data-mail") or "").strip().lower()
        if _EMAIL_RE.fullmatch(raw):
            found.setdefault(raw, None)

    text = soup.get_text(" ", strip=True)
    for address in _EMAIL_RE.findall(text):
        found.setdefault(address.strip().lower(), None)

    # "name [at] company [dot] com" is a deterministic encoding of a stated
    # address, not an inference, so it is decoded rather than discarded.
    for match in _OBFUSCATED_RE.finditer(text):
        candidate = f"{match.group(1)}@{match.group(2)}.{match.group(3)}".lower()
        if _EMAIL_RE.fullmatch(candidate):
            found.setdefault(candidate, None)

    return [
        a for a in found
        if not a.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"))
    ]


def _linkedin_contacts(soup: BeautifulSoup) -> list[ExtractedContact]:
    contacts: list[ExtractedContact] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "linkedin.com/in/" not in href:
            continue
        name = _clean_text(a.get_text(), limit=80)
        if not name:
            continue
        title = None
        parent = a.find_parent(["li", "div", "tr", "article", "section"])
        if parent is not None:
            text = _WS.sub(" ", parent.get_text(" ", strip=True))
            text = _WS.sub(" ", text.replace(name, " ", 1)).strip(" -–—,|·")
            title = _clean_text(text, limit=120)
        contacts.append(
            ExtractedContact(name=name, title=title, email=None, profile_url=href, method="linkedin")
        )
    return contacts


def _mailto_contacts(soup: BeautifulSoup) -> list[ExtractedContact]:
    """Named mailto links, e.g. ``<a href="mailto:j.doe@x.com">Jane Doe, COO</a>``."""
    contacts: list[ExtractedContact] = []
    for a in soup.select("a[href^='mailto:']"):
        match = _MAILTO_RE.match(a.get("href") or "")
        if not match:
            continue
        email = match.group(1).strip()
        if not _EMAIL_RE.fullmatch(email) or is_role_email(email):
            continue
        label = _clean_text(a.get_text(), limit=120)
        name = title = None
        if label and "@" not in label:
            parts = [p.strip() for p in re.split(r"[,–—|]| - ", label) if p.strip()]
            if parts:
                name = parts[0]
                title = parts[1] if len(parts) > 1 else None
        if not name:
            parent = a.find_parent(["li", "div", "tr", "td", "article"])
            if parent is not None:
                text = _WS.sub(" ", parent.get_text(" ", strip=True))
                text = text.replace(email, " ").strip()
                name = _clean_text(text, limit=80)
        contacts.append(
            ExtractedContact(name=name, title=title, email=email, profile_url=None, method="mailto")
        )
    return contacts


# ---------------------------------------------------------------------------
# Plain-text name / title pairs (employer-site people pages only)
# ---------------------------------------------------------------------------
#: A page whose <title> says it is an error or a challenge lists nobody.
_ERROR_TITLE_MARKERS = (
    "not found", "404", "error", "access denied", "forbidden", "just a moment",
    "attention required", "unavailable", "cloudflare", "captcha",
)

#: "Name - Title", "Name, Title", "Name | Title". Not "Label: Value", which is
#: how job ads write "Reports To: Plant Manager".
_PAIR_SEPARATOR = re.compile(r"\s+[-\u2013\u2014]\s+|\s*[\u2013\u2014|]\s*|,\s+")

#: Words that make a line a sentence rather than a bare job title.
_SENTENCE_WORDS = frozenset({
    "the", "is", "are", "was", "were", "we", "our", "you", "your", "if", "can",
    "will", "this", "that", "please", "to", "with", "it", "be", "a", "an", "has",
    "have", "just", "may", "should", "site", "page", "website", "click", "contact",
    "email", "call", "who", "what", "when", "how", "not",
})

#: Words that make a capitalised line a heading or an organisation, not a person:
#: "Our Leadership", "Meet The Team", "Brown Construction Services".
_NOT_PERSON_WORDS = frozenset({
    "our", "meet", "leadership", "management", "executive", "executives", "staff",
    "about", "company", "board", "directors", "officers", "welcome", "team",
    "llc", "inc", "corp", "corporation", "co", "group", "services", "solutions",
    "construction", "electric", "industries", "industrial", "holdings",
    "department", "division", "office", "operations", "careers", "home",
})


#: Every word of a POC title, plus the title words around them. A line holding
#: any of them is a title fragment ("President of Maintenance"), not a name.
_TITLE_WORDS = frozenset(
    word for titles in (P1_TITLES, P2_TITLES, P3_TITLES, P4_TITLES)
    for title in titles for word in title.split()
) | {"vice", "officer", "director", "manager", "chief", "head", "senior", "sr", "vp"}


def _is_name_line(text: str) -> bool:
    words = set(_normalize_title(text).split())
    return (_valid_person_name(text) and _looks_like_person_name(text)
            and not words & (_NOT_PERSON_WORDS | _TITLE_WORDS))


def _is_title_line(text: str) -> bool:
    """A short line that is only a POC title, not a sentence mentioning one."""
    words = text.split()
    if not 1 <= len(words) <= 8 or len(text) > 80:
        return False
    if any(ch in text for ch in "?!@") or text.rstrip().endswith("."):
        return False
    if set(_normalize_title(text).split()) & _SENTENCE_WORDS:
        return False
    return infer_priority(text) is not None


#: Elements a site uses to set a person's name apart from the text around it.
_NAME_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "dt", "th"})


def _names_a_person(element) -> bool:
    classes = " ".join(element.get("class") or []).lower()
    return element.name in _NAME_TAGS or "name" in classes


def _same_card(name_el, title_el) -> bool:
    """The title sits in the name's own element or right beside it."""
    return (name_el is title_el or name_el.parent is title_el.parent
            or title_el.parent is name_el or name_el.parent is title_el)


def _text_pair_contacts(html: str) -> list[ExtractedContact]:
    """Explicit name/title pairs from a people page's text.

    Accepted: one text node "Name - Title" / "Name, Title" / "Name | Title";
    or a name node followed by a title node, when the name is set apart as a
    heading, bold or ``*name*``-classed element in the same card as the title,
    or both lines are in one element ("Jane Smith<br>HR Director"). Two loose
    paragraphs that happen to be adjacent ("Great Benefits" / "General
    Manager") are not a person.
    """
    soup = BeautifulSoup(html, "html.parser")
    page_title = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    if any(marker in page_title for marker in _ERROR_TITLE_MARKERS):
        return []
    # Not <header>/<footer>: themes leave <header> unclosed, which nests the whole
    # page inside it (brownandroot.com, live 2026-09-17). Menus live in <nav>.
    for node in soup(["script", "style", "noscript", "nav", "form", "title", "head"]):
        node.decompose()
    blocks = [
        (text, node.parent)
        for node in soup.find_all(string=True)
        if node.parent is not None and node.parent.name not in ("[document]", "html")
        and (text := _WS.sub(" ", str(node)).strip())
    ]

    contacts: list[ExtractedContact] = []
    for index, (text, element) in enumerate(blocks):
        parts = _PAIR_SEPARATOR.split(text, maxsplit=1)
        if len(parts) == 2 and _is_name_line(parts[0]) and _is_title_line(parts[1]):
            name, title = parts
        elif index + 1 < len(blocks):
            next_text, next_element = blocks[index + 1]
            if not (_is_name_line(text) and _is_title_line(next_text)):
                continue
            if _is_name_line(_PAIR_SEPARATOR.split(next_text, maxsplit=1)[0]):
                continue
            if not (element is next_element
                    or (_names_a_person(element) and _same_card(element, next_element))):
                continue
            name, title = text, next_text
        else:
            continue
        contacts.append(ExtractedContact(name=name.strip(), title=title.strip(),
                                         email=None, profile_url=None, method="text"))
    return contacts


def extract_contacts_from_html(
    html: str | None,
    base_url: str | None = None,
    discovery_stage: str | None = None,
    source_type: str | None = None,
    source_priority: int | None = None,
    plain_text: bool = False,
) -> list[ContactCandidate]:
    """Extract contacts from page HTML.

    ``plain_text=True`` also reads explicit name/title pairs from the page text.
    Only an employer-site people page may set it.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")

    extracted = (
        _jsonld_contacts(soup)
        + _microformat_contacts(soup)
        + _table_contacts(soup)
        + _block_contacts(soup)
        + _mailto_contacts(soup)
        + _linkedin_contacts(soup)
        + (_text_pair_contacts(html) if plain_text else [])
    )

    seen: dict[str, ContactCandidate] = {}
    for item in extracted:
        if not _valid_person_name(item.name):
            continue
        priority = infer_priority(item.title)
        key = item.name.strip().lower()

        email = item.email if item.email and not is_role_email(item.email) else None
        candidate = ContactCandidate(
            name=item.name,
            title=item.title,
            title_priority=priority,
            email=email,
            source_type=source_type,
            source_priority=source_priority,
            discovery_stage=discovery_stage,
            url=base_url,
            profile_url=item.profile_url,
            confidence=_confidence(item, priority),
            raw={
                "extraction": item.method,
                "name": item.name,
                "title": item.title,
                "email": email,
                "profile_url": item.profile_url,
                "page_url": base_url,
            },
        )

        existing = seen.get(key)
        if existing is None:
            seen[key] = candidate
            continue
        # Keep the richer sighting of the same person.
        if candidate.title_priority and not existing.title_priority:
            seen[key] = candidate
        elif candidate.email and not existing.email:
            existing.email = candidate.email
            existing.raw["email"] = candidate.email

    return list(seen.values())


def _confidence(item: ExtractedContact, priority: int | None) -> float:
    score = 0.3
    if item.method == "jsonld":
        score += 0.3
    elif item.method in ("microformat", "table"):
        score += 0.25
    elif item.method in ("block", "mailto"):
        score += 0.2
    elif item.method == "text":
        score += 0.1
    if item.title:
        score += 0.2
    if priority:
        score += 0.2
    if item.email:
        score += 0.1
    return round(min(score, 1.0), 2)
