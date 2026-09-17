"""Module: Email Discovery and email quality classification.

Collects emails that were **explicitly observed** - in structured page markup,
in the job posting itself, or returned by a paid provider. Patterns are never
guessed: no ``first.last@domain`` construction, no permutation testing.

An observed address is evidence, not a trusted contact. Job-portal emails in
particular can be generic, portal-generated or on someone else's domain, so
every address is classified and kept with its provenance - none is dropped for
being weak:

- ``PERSONAL``            an individual's mailbox on the employer's domain;
- ``ROLE``                a shared mailbox on the employer's domain (hr@,
                          careers@, info@ ...), never a person;
- ``PORTAL_GENERATED``    on a job-board / ATS / platform host: not the
                          employer's direct email;
- ``EXTERNAL_UNVERIFIED`` a free-mail address, or the employer's domain is
                          unknown so the address cannot be checked against it;
- ``DOMAIN_MISMATCH``     on a different domain than the identified employer.

Only PERSONAL and ROLE addresses count toward the lead's email status and its
preferred emails. The rest stay as low-confidence evidence.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from nexbase.contacts.extraction import is_role_email
from nexbase.logging_setup import get_logger
from nexbase.pipeline.normalize import (
    ATS_HOST_TOKENS,
    FREE_EMAIL_DOMAINS,
    NON_EMPLOYER_HOSTS,
    extract_host,
    registrable_domain,
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Addresses that are never a prospect.
_JUNK_DOMAINS = frozenset(
    {"example.com", "example.org", "sentry.io", "wixpress.com", "domain.com",
     "email.com", "yourcompany.com", "company.com"}
)

PERSONAL = "PERSONAL"
ROLE = "ROLE"
PORTAL_GENERATED = "PORTAL_GENERATED"
EXTERNAL_UNVERIFIED = "EXTERNAL_UNVERIFIED"
DOMAIN_MISMATCH = "DOMAIN_MISMATCH"

#: Classes that count as the employer's own mailbox.
TRUSTED_CLASSES = frozenset({PERSONAL, ROLE})

#: Ordinal confidence per class (not a probability). A PERSONAL address with no
#: named person beside it is less certain than one attributed to a contact.
_CONFIDENCE = {
    (PERSONAL, True): 0.9, (PERSONAL, False): 0.6, ROLE: 0.7,
    DOMAIN_MISMATCH: 0.3, EXTERNAL_UNVERIFIED: 0.2, PORTAL_GENERATED: 0.1,
}

#: For choosing between two addresses for the same person.
EMAIL_CLASS_STRENGTH = {
    PERSONAL: 4, ROLE: 3, DOMAIN_MISMATCH: 2, EXTERNAL_UNVERIFIED: 1, PORTAL_GENERATED: 0,
}


def _valid(email: str) -> bool:
    if not _EMAIL_RE.fullmatch(email):
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    if domain in _JUNK_DOMAINS:
        return False
    return not domain.endswith((".png", ".jpg", ".gif", ".webp", ".svg"))


def classify_email(email: str, company_domain: str | None) -> str:
    """The email class of ``email`` for an employer on ``company_domain``."""
    host = email.rsplit("@", 1)[-1].lower()
    root = registrable_domain(host) or host
    if root in NON_EMPLOYER_HOSTS or any(token in host for token in ATS_HOST_TOKENS):
        return PORTAL_GENERATED
    company_root = registrable_domain(extract_host(company_domain or ""))
    if root in FREE_EMAIL_DOMAINS or not company_root:
        return EXTERNAL_UNVERIFIED
    if root != company_root:
        return DOMAIN_MISMATCH
    return ROLE if is_role_email(email) else PERSONAL


def email_confidence(email_class: str, named: bool) -> float:
    if email_class == PERSONAL:
        return _CONFIDENCE[(PERSONAL, named)]
    return _CONFIDENCE[email_class]


@dataclass
class ObservedEmail:
    """One explicitly published address, with where it was seen and its class."""

    email: str
    email_class: str
    confidence: float
    #: Where it came from: a portal ("indeed"), a host ("acme.com"), "zoominfo".
    source: str | None = None
    #: JOB_BOARD | COMPANY_WEBSITE | ATS | ZOOMINFO ...
    source_type: str | None = None
    evidence_url: str | None = None
    #: job_posting | page_markup | jsonld | block | text | zoominfo_enrich_contact ...
    extraction_method: str | None = None
    discovery_stage: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None
    #: True/False against the employer's domain; None when no domain is known.
    on_company_domain: bool | None = None
    verification_status: str = "PENDING"

    def as_dict(self) -> dict:
        return {
            "email": self.email, "email_class": self.email_class,
            "confidence": self.confidence, "source": self.source,
            "source_type": self.source_type, "evidence_url": self.evidence_url,
            "extraction_method": self.extraction_method,
            "discovery_stage": self.discovery_stage,
            "contact_name": self.contact_name, "contact_title": self.contact_title,
            "on_company_domain": self.on_company_domain,
            "verification_status": self.verification_status,
        }


@dataclass
class EmailDiscoveryResult:
    #: Every address with its provenance and class, in discovery order.
    observed: list[ObservedEmail] = field(default_factory=list)

    def _of(self, email_class: str) -> list[str]:
        return [o.email for o in self.observed if o.email_class == email_class]

    @property
    def personal(self) -> list[str]:
        return self._of(PERSONAL)

    @property
    def role(self) -> list[str]:
        return self._of(ROLE)

    @property
    def low_confidence(self) -> list[ObservedEmail]:
        """Portal-generated, external and domain-mismatched addresses: evidence only."""
        return [o for o in self.observed if o.email_class not in TRUSTED_CLASSES]

    @property
    def all(self) -> list[str]:
        return self.personal + self.role

    @property
    def preferred(self) -> list[str]:
        """Personal addresses first; role mailboxes only as a fallback."""
        return self.personal or self.role

    @property
    def named(self) -> list[ObservedEmail]:
        """Company-domain addresses attributable to a named person."""
        return [o for o in self.observed if o.contact_name and o.email_class == PERSONAL]

    @property
    def status(self) -> str:
        """The lead's email status, from the employer's own mailboxes only."""
        from nexbase.core.enums import EmailStatus

        if len(self.all) > 1:
            return EmailStatus.MULTIPLE_EMAILS_FOUND.value
        if self.personal:
            return EmailStatus.PERSONAL_EMAIL_FOUND.value
        if self.role:
            return EmailStatus.ROLE_EMAIL_FOUND.value
        if self.observed:
            return EmailStatus.LOW_CONFIDENCE_EMAIL_FOUND.value
        return EmailStatus.NO_PUBLIC_EMAIL_FOUND.value


class EmailDiscovery:
    """Collects and classifies observed email addresses."""

    def __init__(self, logger=None) -> None:
        self.log = logger or get_logger("nexbase.email.discovery")

    def discover(
        self,
        contacts: list[dict],
        extra_emails: list[str | dict] | None = None,
        company_domain: str | None = None,
    ) -> EmailDiscoveryResult:
        """Classify every address.

        ``contacts`` are contact dicts (``email``, ``name``, ``title`` and their
        provenance); ``extra_emails`` are addresses seen without a person, as
        dicts with ``email`` and provenance keys, or bare strings.
        """
        result = EmailDiscoveryResult()
        seen: set[str] = set()
        company_root = registrable_domain(extract_host(company_domain or "")) or None

        def add(raw, provenance: dict, name=None, title=None) -> None:
            if not raw:
                return
            email = str(raw).strip().lower()
            if not _valid(email) or email in seen:
                return
            seen.add(email)
            email_class = classify_email(email, company_domain)
            # A role mailbox is company-level evidence and never a person.
            if email_class == ROLE:
                name = title = None
            root = registrable_domain(email.rsplit("@", 1)[-1])
            result.observed.append(ObservedEmail(
                email=email,
                email_class=email_class,
                confidence=email_confidence(email_class, bool(name)),
                source=provenance.get("source"),
                source_type=provenance.get("source_type"),
                evidence_url=provenance.get("evidence_url"),
                extraction_method=provenance.get("extraction_method"),
                discovery_stage=provenance.get("discovery_stage"),
                contact_name=name,
                contact_title=title,
                on_company_domain=None if company_root is None else root == company_root,
            ))

        for contact in contacts or []:
            c = contact or {}
            # An email a provider filled into a public contact carries the
            # provider's provenance, not the page the person was found on.
            filled = (c.get("field_provenance") or {}).get("email")
            add(c.get("email"), filled or {
                "source": c.get("source"),
                "source_type": c.get("source_type"),
                "evidence_url": c.get("source_url") or c.get("profile_url"),
                "extraction_method": c.get("extraction"),
                "discovery_stage": c.get("discovery_stage"),
            }, name=c.get("name"), title=c.get("title"))
        for email in extra_emails or []:
            if isinstance(email, dict):
                # A previously classified observation keeps its attribution.
                add(email.get("email"), email, name=email.get("contact_name"),
                    title=email.get("contact_title"))
            else:
                add(email, {})

        self.log.info(
            "email_discovery",
            contact_count=len(contacts or []),
            classes=dict(Counter(o.email_class for o in result.observed)),
        )
        return result

