"""Module: Email Discovery.

Collects emails that were **explicitly observed** - in structured page markup,
in the job posting itself, or returned by a paid provider. Patterns are never
guessed: no ``first.last@domain`` construction, no permutation testing. The
brief's rule is absolute ("Never fabricate ... contact details"), and a guessed
address also poisons deliverability.

Every address is classified as shared or personal:

- ``ROLE``          a shared mailbox (hr@, careers@, recruiting@, jobs@, info@ ...),
                    company-level evidence and never a person;
- ``PERSONAL``      an individual's address attributed to a named contact;
- ``UNATTRIBUTED``  an individual-looking address no named person was seen with.

``on_company_domain`` records whether the address is on the employer's own
domain, so an address published by a web agency or a job board is visible as
such rather than silently counted as the company's.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from nexbase.contacts.extraction import is_role_email
from nexbase.logging_setup import get_logger

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Addresses that are never a prospect.
_JUNK_DOMAINS = frozenset(
    {"example.com", "example.org", "sentry.io", "wixpress.com", "domain.com",
     "email.com", "yourcompany.com", "company.com"}
)


def _valid(email: str) -> bool:
    if not _EMAIL_RE.fullmatch(email):
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    if domain in _JUNK_DOMAINS:
        return False
    return not domain.endswith((".png", ".jpg", ".gif", ".webp", ".svg"))


@dataclass
class ObservedEmail:
    """One explicitly published address, with where it was seen.

    Provenance is part of the record, not a side note: an address without a
    source URL cannot be audited, and the client's output spec requires it.
    """

    email: str
    kind: str                      # PERSONAL | ROLE | UNATTRIBUTED
    source_url: str | None = None
    source_portal: str | None = None
    discovery_stage: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None
    #: True/False against the company's domain; None when no domain is known.
    on_company_domain: bool | None = None
    verification_status: str = "PENDING"

    def as_dict(self) -> dict:
        return {
            "email": self.email, "kind": self.kind,
            "source_url": self.source_url, "source_portal": self.source_portal,
            "discovery_stage": self.discovery_stage,
            "contact_name": self.contact_name, "contact_title": self.contact_title,
            "on_company_domain": self.on_company_domain,
            "verification_status": self.verification_status,
        }


@dataclass
class EmailDiscoveryResult:
    personal: list[str] = field(default_factory=list)
    role: list[str] = field(default_factory=list)
    #: Every address with its provenance, in discovery order.
    observed: list[ObservedEmail] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return self.personal + self.role

    @property
    def preferred(self) -> list[str]:
        """Personal addresses first; role mailboxes only as a fallback."""
        return self.personal or self.role

    @property
    def named(self) -> list[ObservedEmail]:
        """Addresses attributable to a named person."""
        return [o for o in self.observed if o.contact_name]

    @property
    def status(self) -> str:
        """The lead's email status, from what was actually observed."""
        from nexbase.core.enums import EmailStatus

        if len(self.all) > 1:
            return EmailStatus.MULTIPLE_EMAILS_FOUND.value
        if self.personal:
            return EmailStatus.PERSONAL_EMAIL_FOUND.value
        if self.role:
            return EmailStatus.ROLE_EMAIL_FOUND.value
        return EmailStatus.NO_PUBLIC_EMAIL_FOUND.value


class EmailDiscovery:
    """Collects observed email addresses for contacts."""

    def __init__(self, logger=None) -> None:
        self.log = logger or get_logger("nexbase.email.discovery")

    def discover(
        self,
        contacts: list[dict],
        extra_emails: list[str | dict] | None = None,
        company_domain: str | None = None,
    ) -> EmailDiscoveryResult:
        result = EmailDiscoveryResult()
        seen: set[str] = set()
        domain = (company_domain or "").lower() or None

        def add(raw, *, source_url=None, source_portal=None,
                stage=None, name=None, title=None) -> None:
            if not raw:
                return
            email = str(raw).strip().lower()
            if not _valid(email) or email in seen:
                return
            seen.add(email)
            role = is_role_email(email)
            (result.role if role else result.personal).append(email)
            host = email.rsplit("@", 1)[-1]
            on_domain = None if domain is None else (
                host == domain or host.endswith("." + domain))
            # A role mailbox is company-level evidence and never a person, so
            # it is recorded without a name even when one was passed in.
            result.observed.append(
                ObservedEmail(
                    email=email,
                    kind="ROLE" if role else ("PERSONAL" if name else "UNATTRIBUTED"),
                    source_url=source_url, source_portal=source_portal,
                    discovery_stage=stage,
                    contact_name=None if role else name,
                    contact_title=None if role else title,
                    on_company_domain=on_domain,
                )
            )

        for contact in contacts or []:
            c = contact or {}
            add(c.get("email"), source_url=c.get("source_url") or c.get("profile_url"),
                source_portal=c.get("source_type"),
                stage=c.get("discovery_stage"),
                name=c.get("name"), title=c.get("title"))
        for email in extra_emails or []:
            if isinstance(email, dict):
                add(email.get("email"), source_url=email.get("source_url"),
                    source_portal=email.get("source_portal"),
                    stage=email.get("discovery_stage"))
            else:
                add(email)

        self.log.info(
            "email_discovery",
            contact_count=len(contacts or []),
            personal=len(result.personal),
            role=len(result.role),
        )
        return result
