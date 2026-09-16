"""Module: Email Verification (ZeroBounce).

Stores verification status + confidence for every email checked, links the log
row to the contact it belongs to, and writes the verdict back onto the contact
so the final lead can filter on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nexbase.config import Settings, get_settings
from nexbase.core.enums import VerificationStatus
from nexbase.logging_setup import get_logger

_STATUS_MAP: dict[str, tuple[VerificationStatus, float]] = {
    "valid": (VerificationStatus.VALID, 0.95),
    "catch-all": (VerificationStatus.RISKY, 0.5),
    "catch_all": (VerificationStatus.RISKY, 0.5),
    "unknown": (VerificationStatus.UNKNOWN, 0.0),
    "invalid": (VerificationStatus.INVALID, 0.95),
    "spamtrap": (VerificationStatus.RISKY, 0.4),
    "abuse": (VerificationStatus.RISKY, 0.4),
    "do_not_mail": (VerificationStatus.RISKY, 0.4),
}


@dataclass
class VerificationResult:
    email: str
    status: str
    confidence: float | None = None
    sub_status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def sendable(self) -> bool:
        return self.status == VerificationStatus.VALID.value


class EmailVerifier:
    """Verifies emails via ZeroBounce."""

    provider = "ZEROBOUNCE"

    def __init__(
        self,
        settings: Settings | None = None,
        repo=None,
        logger=None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repo
        self.log = logger or get_logger("nexbase.email.verification")

    def is_configured(self) -> bool:
        return bool(self.settings.zerobounce_api_key)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    def _call(self, email: str) -> dict[str, Any]:
        # The key travels in a header, not the query string: query strings are
        # written to proxy and server access logs verbatim.
        response = httpx.post(
            f"{self.settings.zerobounce_api_url.rstrip('/')}/validate",
            json={"email": email},
            headers={"Authorization": f"Bearer {self.settings.zerobounce_api_key}"},
            timeout=self.settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    def verify(
        self,
        email: str,
        contact_id: str | None = None,
        company_id: str | None = None,
    ) -> VerificationResult:
        if not self.is_configured():
            self.log.info("email_verify_skipped", email=email, reason="NOT_CONFIGURED")
            return VerificationResult(email=email, status=VerificationStatus.PENDING.value)

        try:
            raw = self._call(email)
        except Exception as exc:
            self.log.warning("email_verify_error", email=email, error=str(exc))
            return VerificationResult(
                email=email,
                status=VerificationStatus.UNKNOWN.value,
                raw={"error": str(exc)},
            )

        status_enum, confidence = _STATUS_MAP.get(
            str(raw.get("status", "")).lower(), (VerificationStatus.UNKNOWN, 0.0)
        )
        result = VerificationResult(
            email=email,
            status=status_enum.value,
            confidence=confidence,
            sub_status=raw.get("sub_status"),
            raw=raw,
        )

        self._persist(result, contact_id, company_id)
        self.log.info(
            "email_verified",
            email=email,
            status=result.status,
            confidence=result.confidence,
        )
        return result

    def verify_contacts(self, contacts: list[dict], company_id: str | None = None):
        """Verify each contact's email and write the verdict back onto the contact."""
        results: list[VerificationResult] = []
        for contact in contacts:
            email = (contact or {}).get("email")
            if not email:
                continue
            result = self.verify(email, contact_id=contact.get("id"), company_id=company_id)
            contact["verification_status"] = result.status
            contact["verification_confidence"] = result.confidence
            results.append(result)
        return results

    def verify_emails(self, emails: list[str]) -> list[VerificationResult]:
        return [self.verify(email) for email in emails]

    # ------------------------------------------------------------------
    def _persist(
        self,
        result: VerificationResult,
        contact_id: str | None,
        company_id: str | None,
    ) -> None:
        if self.repo is None:
            return

        self.repo.insert_email_verification(
            {
                "contact_id": contact_id,
                "company_id": company_id,
                "email": result.email,
                "provider": self.provider,
                "status": result.status,
                "confidence": result.confidence,
                "raw_result": result.raw,
            }
        )

        if contact_id:
            self.repo.update_contact(
                contact_id,
                {
                    "verification_status": result.status,
                    "verification_confidence": result.confidence,
                },
            )
