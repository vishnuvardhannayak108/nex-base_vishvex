"""LinkedIn applicant-count signal.

Brief: *"When publicly available, prioritize LinkedIn jobs with 20 or fewer
applicants. Treat applicant count as a signal; do not bypass access controls
or platform restrictions. Missing applicant counts do not automatically
disqualify a company."*

JobSpy exposes no applicant count on any site (verified: no `applicant`
field anywhere in the package), so this reads the figure from the **public,
logged-out** LinkedIn job posting that the discovery step already linked to.

Deliberate constraints, per the brief:

* Only the public guest view is requested. No login, no cookies, no session
  reuse, nothing behind an access control.
* A missing or unparseable count yields ``None``, never a guess and never a
  disqualification.
* Enrichment is opt-in and budgeted, because it costs one fetch per posting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from nexbase.access.fetcher import AccessLayer
from nexbase.core.models import RawJob
from nexbase.logging_setup import get_logger

#: "27 applicants", "Over 200 applicants", "Be among the first 25 applicants"
_APPLICANTS_RE = re.compile(
    r"(?:over\s+)?(\d[\d,]*)\s*(?:\+\s*)?applicants?", re.IGNORECASE
)
_FIRST_N_RE = re.compile(
    r"be among the first\s+(\d[\d,]*)\s+applicants?", re.IGNORECASE
)

_LINKEDIN_JOB_HOSTS = ("linkedin.com",)


@dataclass
class ApplicantSignal:
    job_url: str
    applicant_count: int | None
    is_upper_bound: bool = False
    reason: str | None = None


def parse_applicant_count(html: str | None) -> ApplicantSignal | None:
    """Extract an applicant count from public LinkedIn job HTML."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    candidates: list[str] = []
    for selector in (
        "span.num-applicants__caption",
        "figcaption.num-applicants__caption",
        ".jobs-unified-top-card__applicant-count",
        ".job-details-jobs-unified-top-card__primary-description-container",
        ".topcard__flavor--metadata",
    ):
        for el in soup.select(selector):
            candidates.append(el.get_text(" ", strip=True))

    if not candidates:
        text = soup.get_text(" ", strip=True)
        candidates = [text[:5000]]

    for text in candidates:
        first = _FIRST_N_RE.search(text)
        if first:
            return ApplicantSignal(
                job_url="",
                applicant_count=int(first.group(1).replace(",", "")),
                is_upper_bound=True,
                reason="BE_AMONG_FIRST",
            )
        match = _APPLICANTS_RE.search(text)
        if match:
            value = int(match.group(1).replace(",", ""))
            over = text.lower().strip().startswith("over") or " over " in text.lower()
            return ApplicantSignal(
                job_url="",
                applicant_count=value,
                is_upper_bound=False,
                reason="OVER_THRESHOLD" if over else "EXACT",
            )
    return None


def is_linkedin_job(url: str | None) -> bool:
    return bool(url) and any(host in url for host in _LINKEDIN_JOB_HOSTS)


class LinkedInApplicantEnricher:
    """Adds applicant counts to LinkedIn-sourced jobs, within a fetch budget."""

    def __init__(
        self,
        access: AccessLayer | None = None,
        logger=None,
        max_fetches: int = 50,
    ) -> None:
        self.access = access or AccessLayer()
        self.log = logger or get_logger("nexbase.discovery.linkedin_signal")
        self.max_fetches = max_fetches
        self.fetches_used = 0

    def enrich(self, jobs: list[RawJob]) -> list[RawJob]:
        """Populate ``applicant_count`` in place where publicly available."""
        targets = [
            j
            for j in jobs
            if j.applicant_count is None
            and (j.source_site == "linkedin" or is_linkedin_job(j.apply_url))
            and (j.apply_url or j.application_url)
        ]
        if not targets:
            return jobs

        self.log.info(
            "linkedin_signal_start", candidates=len(targets), budget=self.max_fetches
        )
        for job in targets:
            if self.fetches_used >= self.max_fetches:
                self.log.info("linkedin_signal_budget_exhausted", used=self.fetches_used)
                break
            url = job.apply_url or job.application_url
            if not is_linkedin_job(url):
                continue
            self.fetches_used += 1
            try:
                page = self.access.fetch(url)
            except Exception as exc:
                self.log.debug("linkedin_signal_fetch_error", url=url, error=str(exc))
                continue
            if not page.ok:
                continue
            signal = parse_applicant_count(page.html)
            if signal is None:
                continue
            job.applicant_count = signal.applicant_count
            job.raw["applicant_signal"] = {
                "count": signal.applicant_count,
                "is_upper_bound": signal.is_upper_bound,
                "reason": signal.reason,
                "url": url,
            }
            self.log.info(
                "linkedin_signal_found", url=url, applicants=signal.applicant_count
            )

        found = sum(1 for j in targets if j.applicant_count is not None)
        self.log.info(
            "linkedin_signal_complete",
            candidates=len(targets),
            found=found,
            fetches=self.fetches_used,
        )
        return jobs
