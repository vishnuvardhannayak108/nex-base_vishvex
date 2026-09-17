"""Module: Email Verification (ZeroBounce), after POC ranking.

ZeroBounce single email validator, as documented (zerobounce.net/docs, "Single
Email Validator - Real time (v2)"): ``/v2/validate`` with ``api_key``, ``email``
and ``ip_address``, sent as an ``application/x-www-form-urlencoded`` POST body so
the key never appears in a URL or access log. Response ``status`` is one of
valid, invalid, catch-all, unknown, spamtrap, abuse, do_not_mail, with a
``sub_status``; a failed call returns ``{"error": "..."}``. Unknown results
consume no credit.

Only a ranked POC's email on the employer's own domain (PERSONAL or ROLE class) is
verified; portal-generated, external and mismatched addresses are SKIPPED. Paid
verification is off unless ``EMAIL_VERIFICATION_ENABLED`` is set, is capped per
run, and reuses a final verdict (VALID / INVALID / RISKY) inside the refresh
window. A provider failure never becomes a verdict: the email stays PENDING.
Nothing here sends email.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from nexbase.config import Settings, get_settings
from nexbase.core.enums import VerificationStatus
from nexbase.email.discovery import TRUSTED_CLASSES, classify_email
from nexbase.logging_setup import get_logger

PROVIDER = "ZEROBOUNCE"

#: ZeroBounce status -> NexBase status. The raw status and sub_status are kept.
STATUS_MAP = {
    "valid": VerificationStatus.VALID,
    "invalid": VerificationStatus.INVALID,
    "catch-all": VerificationStatus.RISKY,
    "spamtrap": VerificationStatus.RISKY,
    "abuse": VerificationStatus.RISKY,
    "do_not_mail": VerificationStatus.RISKY,
    "unknown": VerificationStatus.UNKNOWN,
}
#: Verdicts worth reusing; UNKNOWN is re-checked (it costs no credit).
FINAL = frozenset({VerificationStatus.VALID.value, VerificationStatus.INVALID.value,
                   VerificationStatus.RISKY.value})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class VerificationResult:
    email: str
    status: str
    reason: str | None = None
    provider_status: str | None = None
    sub_status: str | None = None
    billable: bool = False
    called: bool = False
    at: str = field(default_factory=_now)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "provider": PROVIDER,
                "provider_status": self.provider_status, "sub_status": self.sub_status,
                "billable": self.billable, "called": self.called, "at": self.at}


class EmailVerifier:
    """One documented ZeroBounce call per email; no retries on paid calls."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None,
                 logger=None) -> None:
        self.settings = settings or get_settings()
        self.client = client
        self.log = logger or get_logger("nexbase.email.verification")

    def is_configured(self) -> bool:
        return bool(self.settings.zerobounce_api_key)

    def verify(self, email: str) -> VerificationResult:
        if not self.is_configured():
            return VerificationResult(email, VerificationStatus.PENDING.value, "NOT_CONFIGURED")
        data = {"api_key": self.settings.zerobounce_api_key, "email": email, "ip_address": ""}
        url = f"{self.settings.zerobounce_api_url.rstrip('/')}/validate"
        try:
            client = self.client or httpx
            response = client.post(url, data=data, timeout=self.settings.request_timeout_seconds)
        except httpx.TimeoutException:
            return VerificationResult(email, VerificationStatus.PENDING.value,
                                      "PROVIDER_TIMEOUT", called=True)
        except httpx.HTTPError as exc:
            return VerificationResult(email, VerificationStatus.PENDING.value,
                                      f"PROVIDER_ERROR:{type(exc).__name__}", called=True)
        if response.status_code in (401, 403):
            return VerificationResult(email, VerificationStatus.PENDING.value,
                                      "PROVIDER_UNAVAILABLE", called=True)
        if response.status_code == 429:
            return VerificationResult(email, VerificationStatus.PENDING.value,
                                      "PROVIDER_RATE_LIMITED", called=True)
        try:
            response.raise_for_status()
            raw = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return VerificationResult(email, VerificationStatus.PENDING.value,
                                      f"PROVIDER_ERROR:{type(exc).__name__}", called=True)
        if not isinstance(raw, dict) or raw.get("error"):
            # Documented failure: "Invalid API Key or your account ran out of credits".
            return VerificationResult(email, VerificationStatus.PENDING.value,
                                      "PROVIDER_UNAVAILABLE", called=True,
                                      raw=raw if isinstance(raw, dict) else {})
        provider_status = str(raw.get("status") or "").lower()
        status = STATUS_MAP.get(provider_status)
        if status is None:
            return VerificationResult(email, VerificationStatus.PENDING.value,
                                      f"UNRECOGNIZED_STATUS:{provider_status}", called=True,
                                      raw=raw)
        return VerificationResult(
            email, status.value, None, provider_status, raw.get("sub_status") or None,
            billable=status is not VerificationStatus.UNKNOWN, called=True, raw=raw)


class VerificationStage:
    """Verifies ranked POC emails for a run's final leads, within budget."""

    #: Failures that stop further calls for the rest of the run.
    STOPPING = ("PROVIDER_UNAVAILABLE", "PROVIDER_RATE_LIMITED")

    def __init__(self, repo, settings: Settings | None = None, verifier: EmailVerifier | None = None,
                 logger=None) -> None:
        self.repo = repo
        self.settings = settings or get_settings()
        self.verifier = verifier or EmailVerifier(self.settings, logger=logger)
        self.log = logger or get_logger("nexbase.email.verification")
        self.stats = {"verified": 0, "cached": 0, "skipped": 0, "pending": 0, "billable": 0,
                      "statuses": {}}
        self._calls = 0
        self._stopped: str | None = None
        self._run_results: dict[str, VerificationResult] = {}

    def verify_lead(self, contacts: list[dict], domain: str | None, company_id) -> None:
        """Set ``verification`` (and ``verification_status``) on every contact with an email."""
        for contact in contacts:
            email = (contact.get("email") or "").strip().lower()
            if not email:
                continue
            result = self._result(email, domain, company_id, contact.get("id"))
            contact["verification_status"] = result.status
            contact["verification"] = result.as_dict()
            counts = self.stats["statuses"]
            counts[result.status] = counts.get(result.status, 0) + 1

    def _result(self, email, domain, company_id, contact_id) -> VerificationResult:
        email_class = classify_email(email, domain)
        if email_class not in TRUSTED_CLASSES:
            self.stats["skipped"] += 1
            result = VerificationResult(email, VerificationStatus.SKIPPED.value, f"EMAIL_CLASS:{email_class}")
        elif email in self._run_results:
            result = self._run_results[email]
        elif (cached := self._cached(email)) is not None:
            self.stats["cached"] += 1
            result = self._run_results[email] = cached
        else:
            result = self._call(email)
            if result.called:
                self.repo.insert_email_verification({
                    "contact_id": contact_id, "company_id": company_id, "email": email,
                    "provider": PROVIDER, "status": result.status, "confidence": None,
                    "raw_result": {**result.as_dict(), "response": result.raw}})
            if result.status in FINAL | {VerificationStatus.UNKNOWN.value}:
                self._run_results[email] = result
        if result.status == VerificationStatus.PENDING.value:
            self.stats["pending"] += 1
        elif contact_id:
            self.repo.update_contact(contact_id, {"verification_status": result.status})
        return result

    def _call(self, email) -> VerificationResult:
        if not self.settings.email_verification_enabled:
            return VerificationResult(email, VerificationStatus.PENDING.value, "DISABLED")
        if not self.verifier.is_configured():
            return VerificationResult(email, VerificationStatus.PENDING.value, "NOT_CONFIGURED")
        if self._stopped:
            return VerificationResult(email, VerificationStatus.PENDING.value, self._stopped)
        if self._calls >= self.settings.email_verification_max_per_run:
            return VerificationResult(email, VerificationStatus.PENDING.value, "BUDGET_EXHAUSTED")
        self._calls += 1
        result = self.verifier.verify(email)
        if result.reason in self.STOPPING:
            self._stopped = result.reason
            self.log.warning("email_verification_stopped", reason=result.reason)
        if result.status != VerificationStatus.PENDING.value:
            self.stats["verified"] += 1
            self.stats["billable"] += result.billable
        return result

    def _cached(self, email) -> VerificationResult | None:
        if not getattr(self.repo, "configured", False):
            return None
        rows = self.repo.select("email_verification_logs", "status, raw_result, created_at",
                                filters={"email": email, "provider": PROVIDER},
                                order_by="created_at", limit=1)
        if not rows or rows[0].get("status") not in FINAL:
            return None
        try:
            created = datetime.fromisoformat(str(rows[0]["created_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(
                days=self.settings.email_verification_refresh_days):
            return None
        raw = rows[0].get("raw_result") or {}
        return VerificationResult(email, rows[0]["status"], "CACHED", raw.get("provider_status"),
                                  raw.get("sub_status"), at=raw.get("at") or created.isoformat())
