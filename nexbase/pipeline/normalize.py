"""Normalization: the first stage after discovery.

Standardizes company name, domain, job title, location (city, state, country,
remote) and posting date (UTC, with its precision), and screens out postings
that cannot become a lead: no employer name, outside the US, or not full-time.
Never invents values: missing fields stay null.

**Domain resolution is the load-bearing part.** Job boards report a *profile*
URL as the employer URL - Indeed gives ``indeed.com/cmp/...``, LinkedIn gives
``linkedin.com/company/...`` - so naively deriving a domain from it collapses
every Indeed-sourced company onto ``indeed.com`` and breaks deduplication,
contact discovery and enrichment keying at once. A domain is therefore only
accepted from a source that genuinely identifies the employer's own site, and
aggregator hosts are rejected outright.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from nexbase.core.geo import us_state_of
from nexbase.core.models import RawJob
from nexbase.core.timeutils import coerce_datetime
from nexbase.logging_setup import get_logger

_WHITESPACE = re.compile(r"\s+")
_LEGAL_SUFFIX = re.compile(
    r"[\s,]*\b("
    r"inc|llc|l\.l\.c|corp|corporation|incorporated|ltd|limited|co|company|"
    r"gmbh|ag|plc|llp|lp|pty|pte|sa|sas|bv|nv|oy|ab|as|s\.r\.o|s\.p\.a|srl|kg|kk"
    r")\b\.?$",
    re.IGNORECASE,
)
_PUNCT_TAIL = re.compile(r"[\s,.\-–—/|]+$")

#: Hosts that are job boards, aggregators, ATS vendors or link shorteners.
#: A domain from any of these identifies the *platform*, never the employer.
NON_EMPLOYER_HOSTS: frozenset[str] = frozenset(
    {
        # Aggregators / boards
        "indeed.com", "linkedin.com", "ziprecruiter.com", "glassdoor.com",
        "google.com", "monster.com", "simplyhired.com", "careerbuilder.com",
        "dice.com", "snagajob.com", "craigslist.org", "jobcase.com",
        "talent.com", "adzuna.com", "jooble.org", "neuvoo.com", "lensa.com",
        "getwork.com", "recruit.net", "jobs2careers.com", "upward.net",
        "flexjobs.com", "wellfound.com", "angel.co", "builtin.com",
        "themuse.com", "hired.com", "remoteok.com", "weworkremotely.com",
        "ycombinator.com", "welcometothejungle.com",
        # Aggregators evaluated during the 2026-09-14 portal expansion. Every
        # one of these serves job URLs on its own host, so without them an
        # aggregator's domain is mistaken for the employer's.
        "postjobfree.com", "careerjet.com", "jobrapido.com", "jora.com",
        "nexxt.com", "ihire.com", "myjobhelper.com", "joblist.com",
        "ziprecruiter.co.uk", "workcircle.com", "trovit.com", "mitula.com",
        # ATS / HR platforms
        "greenhouse.io", "boards.greenhouse.io", "lever.co", "jobs.lever.co",
        "ashbyhq.com", "workday.com", "myworkdayjobs.com", "smartrecruiters.com",
        "bamboohr.com", "workable.com", "breezy.hr", "jazzhr.com",
        "applytojob.com", "recruitee.com", "personio.de", "teamtailor.com",
        "icims.com", "taleo.net", "successfactors.com", "oraclecloud.com",
        "paylocity.com", "paycomonline.net", "adp.com", "dayforcehcm.com",
        "ukg.com", "ultipro.com", "jobvite.com", "rippling.com", "gem.com",
        "pinpointhq.com", "hrmos.co", "keka.com", "darwinbox.com",
        # Published as Indeed "company websites" in the 2026-09-17 live smoke.
        "catsone.com", "careerplug.com", "entertimeonline.com",
        # Social / misc
        "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
        "bit.ly", "tinyurl.com", "goo.gl", "t.co", "lnkd.in",
    }
)

#: Public mailbox providers - never an employer domain.
FREE_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
        "icloud.com", "mail.com", "protonmail.com", "gmx.com", "live.com",
        "msn.com", "me.com", "yandex.com", "zoho.com",
    }
)

#: Substrings that mark a host as recruiting infrastructure rather than an
#: employer, catching the long tail of ATS vendors not listed individually
#: (recruit.hirebridge.com, workforcenow.adp.com, apply.workable.com, ...).
ATS_HOST_TOKENS: tuple[str, ...] = (
    "hirebridge", "applicantpro", "applicantstack", "clearcompany",
    "workforcenow", "myworkday", "brassring", "silkroad", "clearstar",
    "jobappnetwork", "hiringthing", "smartsearchonline", "trakstar",
    "exacthire", "ninjagig", "hyrell", "bamboohr", "isolvedhire",
    "paycor", "kronos", "cornerstoneondemand", "peoplefluent", "ihire",
    "snaphire", "jobtarget", "talentreef", "fountain", "jobscore",
    "hiringplatform", "recruiterbox", "workbrightonboard", "greenhouse",
    "smartrecruiters", "successfactors", "taleo", "icims", "jazzhr",
    "applytojob", "myworkdayjobs", "ultipro", "dayforcehcm", "paycomonline",
)

#: Structural TLD rules. A hand-maintained allowlist was silently discarding
#: real employers on newer TLDs (.builders, .tools, .farm, .coop ...), which is
#: worse than the corruption it guarded against: the domain vanished with no
#: warning. Validation is now structural, and the specific corruption that
#: motivated the list ("indeed.comnone") is caught by _CORRUPT_PREFIXES below.
_MIN_TLD_LEN, _MAX_TLD_LEN = 2, 24


def has_valid_tld(host: str) -> bool:
    """True when the host ends in something structurally shaped like a TLD."""
    if "." not in host:
        return False
    tld = host.rsplit(".", 1)[-1]
    return tld.isalpha() and _MIN_TLD_LEN <= len(tld) <= _MAX_TLD_LEN


def is_corrupted_host(host: str) -> bool:
    """True for a platform domain with trailing junk glued on.

    Job-board exports contain hosts like "indeed.comnone" and
    "linkedin.comnull". They are structurally valid, so only knowing that a
    real platform domain is a prefix of them catches it.
    """
    if not host:
        return False
    lowered = host.lower()
    return any(
        lowered.startswith(platform) and lowered != platform
        and not lowered.startswith(f"{platform}.")
        for platform in NON_EMPLOYER_HOSTS
    )


def normalize_text(value: str | None) -> str:
    """Lowercase, trim, and collapse internal whitespace."""
    if not value:
        return ""
    return _WHITESPACE.sub(" ", str(value).strip().lower())


def _fold(value: str) -> str:
    """HTML entities decoded and accents removed, so spellings compare equal."""
    text = unicodedata.normalize("NFKD", html.unescape(value))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_title(value: str | None) -> str:
    """Lowercase title with entities, dashes and spacing made uniform.

    Deliberately shallow: "Machinist (1st Shift)" and "Machinist (2nd Shift)"
    are different openings, so nothing but formatting is removed.
    """
    if not value:
        return ""
    text = re.sub(r"[\u2010-\u2015]", "-", _fold(str(value)))
    return _PUNCT_TAIL.sub("", normalize_text(text))


def normalize_company_name(value: str | None) -> str:
    """Normalize a company name and strip trailing legal suffixes.

    "&" and "and", accents, apostrophes and punctuation are folded, and legal
    suffixes are stripped repeatedly, so "Acme Manufacturing Co., Inc.",
    "ACME Manufacturing" and "Acmé Manufacturing" reduce to the same name.
    """
    if not value:
        return ""
    normalized = normalize_text(_fold(str(value)).replace("&", " and "))
    normalized = re.sub(r"['\u2019`]", "", normalized)
    normalized = _PUNCT_TAIL.sub("", normalized)
    for _ in range(3):
        stripped = _LEGAL_SUFFIX.sub("", normalized)
        stripped = _PUNCT_TAIL.sub("", stripped)
        if stripped == normalized:
            break
        normalized = stripped
    normalized = re.sub(r"[^\w\s]", " ", normalized)
    normalized = re.sub(r"^the\s+", "", _WHITESPACE.sub(" ", normalized).strip())
    return normalized


def extract_host(url: str | None) -> str:
    """Return the bare lowercased host of a URL, without ``www.``."""
    if not url:
        return ""
    candidate = str(url).strip()
    if not candidate:
        return ""
    if "//" not in candidate:
        candidate = f"//{candidate}"
    try:
        host = urlsplit(candidate).netloc
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0].lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def registrable_domain(host: str) -> str:
    """Collapse a host to its registrable domain (best-effort, no PSL dependency)."""
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Handle common two-part public suffixes (co.uk, com.au, ...).
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "gov", "edu", "ac"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_employer_domain(host: str) -> bool:
    """True when a host plausibly belongs to the employer rather than a platform."""
    if not host or "." not in host:
        return False
    if not has_valid_tld(host):
        return False
    if any(token in host for token in ATS_HOST_TOKENS):
        return False
    if is_corrupted_host(host):
        return False
    root = registrable_domain(host)
    if is_corrupted_host(root):
        return False
    return root not in NON_EMPLOYER_HOSTS and root not in FREE_EMAIL_DOMAINS


def normalize_domain(url: str | None) -> str:
    """Extract an *employer* domain from a URL, or "" if it is a platform URL."""
    host = extract_host(url)
    if not host:
        return ""
    # The token/TLD checks look at the full host, so a subdomain like
    # recruit.hirebridge.com is rejected before it collapses to a root.
    if not is_employer_domain(host):
        return ""
    root = registrable_domain(host)
    return root if is_employer_domain(root) else ""


def domain_from_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    host = email.rsplit("@", 1)[-1].strip().lower()
    return normalize_domain(host)


def resolve_domain(raw: RawJob) -> tuple[str, str]:
    """Pick the best available employer domain and say where it came from.

    Precedence, strongest evidence first:
      1. ``company_website`` (Indeed's ``company_url_direct`` etc.)
      2. ``company_url`` when it is not an aggregator/ATS host
      3. the apply/application URL when it is on the employer's own site
      4. an observed email address's domain
    Returns ``("", "NONE")`` when nothing qualifies. Never guesses.
    """
    candidates: list[tuple[str | None, str]] = [
        (raw.company_website, "COMPANY_WEBSITE"),
        (raw.company_url, "COMPANY_URL"),
        (raw.application_url, "APPLICATION_URL"),
        (raw.apply_url, "APPLY_URL"),
    ]
    for value, origin in candidates:
        domain = normalize_domain(value)
        if domain:
            return domain, origin

    for email in raw.observed_emails or []:
        domain = domain_from_email(email)
        if domain:
            return domain, "OBSERVED_EMAIL"

    return "", "NONE"


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)
_LOCATION_PREFIX = re.compile(r"^(?:hybrid|remote|on-?site|onsite)\s+(?:in|-)\s+", re.IGNORECASE)
_US_NAMES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}

#: Location text that names a country other than the US.
NON_US_MARKERS = (
    "canada", "united kingdom", "india", "australia", "germany", "france",
    "mexico", "brazil", "ireland", "singapore", "philippines", "netherlands",
    "spain", "italy", "poland", "japan", "china", "remote, uk", ", uk", ", ca,",
)


@dataclass(frozen=True)
class NormalizedLocation:
    city: str | None
    state: str | None
    #: ISO-style country code when stated or implied by a US state, else None.
    country: str | None
    remote: bool

    @property
    def key(self) -> str:
        """Canonical lower-case form: "toledo, oh", "oh", "remote" or ""."""
        if self.state:
            return f"{self.city}, {self.state}".lower() if self.city else self.state.lower()
        return "remote" if self.remote else ""


def normalize_country(country: str | None) -> str | None:
    if not country:
        return None
    value = str(country).strip().lower()
    if value in _US_NAMES:
        return "US"
    return value.upper() if len(value) == 2 and value.isalpha() else None


def normalize_location(location: str | None, country: str | None = None,
                       is_remote: bool | None = None) -> NormalizedLocation:
    """City, state and country from however a source wrote the location."""
    text = _WHITESPACE.sub(" ", _ZIP.sub("", str(location or ""))).strip(" ,")
    state = us_state_of(text)
    city = None
    if state:
        first = _LOCATION_PREFIX.sub("", text.split(",")[0]).strip()
        if first and us_state_of(first) != state and first.lower() not in _US_NAMES:
            city = first.title() if first.isupper() or first.islower() else first
    code = normalize_country(country)
    lowered = text.lower()
    if code is None:
        words = set(re.findall(r"[a-z]+", lowered.replace(".", "")))
        if state or "united states" in lowered or words & {"us", "usa"}:
            code = "US"
        elif any(marker in lowered for marker in NON_US_MARKERS):
            code = "NON_US"
    return NormalizedLocation(city=city, state=state, country=code,
                              remote=bool(is_remote) or bool(_REMOTE.search(text)))


def is_us_location(country: str | None, location: str | None) -> bool:
    """True unless the posting positively identifies a non-US country."""
    return normalize_location(location, country).country in (None, "US")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def normalize_posted_at(value) -> tuple[datetime | None, str | None]:
    """``(posted_at in UTC, precision)``; precision is "DAY" or "TIME".

    Several sources publish only a date (JobSpy's Indeed and LinkedIn rows,
    Workable, BambooHR), which arrives as midnight. Counting such a posting's
    age in hours would call a job posted on the 2nd "14.5 days old" at noon on
    the 16th, so the precision travels with the timestamp.
    """
    posted = coerce_datetime(value)
    if posted is None:
        return None, None
    posted = posted.replace(tzinfo=timezone.utc) if posted.tzinfo is None else posted.astimezone(timezone.utc)
    midnight = (posted.hour, posted.minute, posted.second, posted.microsecond) == (0, 0, 0, 0)
    return posted, "DAY" if midnight else "TIME"


@dataclass
class NormalizedJob:
    source_type: str
    source_priority: int
    source_site: str | None
    external_id: str | None
    title: str | None
    title_normalized: str
    company_name: str | None
    company_name_normalized: str
    company_url: str | None
    company_website: str | None
    domain: str
    domain_source: str
    location: str | None
    location_normalized: str
    description: str | None
    posted_at: datetime | None
    posting_date: str | None
    application_url: str | None
    apply_url: str | None
    ats_platform: str | None
    country: str | None
    employment_type: str | None
    is_remote: bool | None
    company_industry: str | None
    search_industry: str | None
    company_employee_count: str | None
    applicant_count: int | None
    observed_emails: list[str]
    raw: dict
    provenance: dict = field(default_factory=dict)
    city: str | None = None
    state: str | None = None
    #: "DAY" when the source published only a date, "TIME" for a timestamp.
    date_precision: str | None = None
    #: Why normalization screened this posting out; None when it is in scope.
    screen_reason: str | None = None

    @property
    def evidence_url(self) -> str | None:
        return self.application_url or self.apply_url

    @property
    def is_actionable(self) -> bool:
        """In scope: an employer name and in the US."""
        return self.screen_reason is None


def screen_reason(job: NormalizedJob) -> str | None:
    """Why a posting cannot become a lead, or None.

    A posting without an employer name has nothing to research or contact and
    would manufacture a phantom company. A posting outside the US is out of
    scope however fresh it is.
    """
    if not job.company_name_normalized:
        return "NO_COMPANY_NAME"
    if job.country not in (None, "US"):
        return "NOT_US"
    return None


def normalize_job(raw: RawJob) -> NormalizedJob:
    """Normalize a single raw job record and screen it."""
    posted_at, precision = normalize_posted_at(
        raw.posted_at or (raw.raw.get("date_posted") if raw.raw else None))
    posting_date = posted_at.strftime("%Y-%m-%d") if posted_at else None
    domain, domain_source = resolve_domain(raw)
    location = normalize_location(raw.location, raw.country, raw.is_remote)

    job = NormalizedJob(
        source_type=raw.source_type,
        source_priority=raw.source_priority,
        source_site=raw.source_site,
        external_id=raw.external_id,
        title=raw.title,
        title_normalized=normalize_title(raw.title),
        company_name=raw.company_name,
        company_name_normalized=normalize_company_name(raw.company_name),
        company_url=raw.company_url,
        company_website=raw.company_website,
        domain=domain,
        domain_source=domain_source,
        location=raw.location,
        location_normalized=location.key,
        description=raw.description,
        posted_at=posted_at,
        posting_date=posting_date,
        application_url=raw.application_url,
        apply_url=raw.apply_url,
        ats_platform=raw.ats_platform,
        country=location.country,
        employment_type=raw.employment_type,
        is_remote=location.remote,
        company_industry=raw.company_industry,
        search_industry=raw.search_industry,
        company_employee_count=raw.company_employee_count,
        applicant_count=raw.applicant_count,
        observed_emails=list(raw.observed_emails or []),
        raw=raw.raw or {},
        provenance=dict(raw.provenance or {}),
        city=location.city,
        state=location.state,
        date_precision=precision,
    )
    job.screen_reason = screen_reason(job)
    return job


class Normalizer:
    """Applies field standardization across a batch of raw jobs."""

    def __init__(self, logger=None) -> None:
        self.log = logger or get_logger("nexbase.pipeline.normalize")

    def normalize(self, raw_jobs: list[RawJob]) -> tuple[list[NormalizedJob], list[NormalizedJob]]:
        """Return ``(actionable, discarded)``.

        Discarded rows carry ``screen_reason``; they are returned rather than
        dropped silently so the caller can record why.
        """
        actionable: list[NormalizedJob] = []
        discarded: list[NormalizedJob] = []
        for raw in raw_jobs:
            job = normalize_job(raw)
            (actionable if job.is_actionable else discarded).append(job)

        reasons: dict[str, int] = {}
        for job in discarded:
            key = job.screen_reason.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        self.log.info(
            "normalization_complete",
            jobs=len(actionable),
            with_domain=sum(1 for j in actionable if j.domain),
            with_state=sum(1 for j in actionable if j.state),
            dated=sum(1 for j in actionable if j.posted_at),
            discarded=reasons,
        )
        return actionable, discarded
