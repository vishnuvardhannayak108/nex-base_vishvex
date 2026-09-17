"""Module: Contact Ranking & Selection.

Priority ladder (P1 owner/CEO .. P4 plant/Ops manager) is strictly enforced.
Job-type relevance provides a tie-breaking boost within a priority.

Brief: *"Return up to 3+ meaningful decision-makers or hiring influencers
where available. Do not force weak contacts simply to hit a quota."*
Three is therefore a **target, not a cap** - more are returned when they are
genuinely meaningful, and fewer (including zero) when they are not.
"""
from __future__ import annotations

from dataclasses import dataclass

from nexbase.config import Settings, get_settings
from nexbase.contacts.extraction import infer_priority
from nexbase.contacts.models import ContactCandidate
from nexbase.enrichment.waterfall import email_strength, source_rank
from nexbase.logging_setup import get_logger

_STAGE_ORDER = {"SAME_SOURCE": 0, "OTHER_SOURCE": 1, "PUBLIC_WEB": 2, "ENRICHMENT": 3}


@dataclass
class RankingContext:
    hiring_priority: int | None = None


def dominant_hiring_priority(job_titles: list[str | None]) -> int | None:
    """Infer the most senior hiring function from the company's open roles."""
    found = {p for p in (infer_priority(t) for t in job_titles) if p is not None}
    return min(found) if found else None


def rank_contacts(
    candidates: list[ContactCandidate],
    context: RankingContext | None = None,
) -> list[ContactCandidate]:
    """Sort candidates strictly by P1..P4, then by relevance + confidence."""
    context = context or RankingContext()

    for c in candidates:
        base = (5 - c.priority) * 20.0

        relevance = 0.0
        if context.hiring_priority is not None and c.title_priority is not None:
            gap = abs(c.title_priority - context.hiring_priority)
            if gap == 0:
                relevance = 10.0
            elif gap == 1:
                relevance = 5.0

        stage_penalty = _STAGE_ORDER.get(c.discovery_stage or "", 2) * 1.5
        email_bonus = 4.0 if c.email else 0.0

        c.rank_score = round(
            base + relevance + (c.confidence * 8.0) + email_bonus - stage_penalty, 2
        )

    return sorted(candidates, key=lambda c: (c.priority, -(c.rank_score or 0.0)))


def select_contacts(
    candidates: list[ContactCandidate],
    limit: int | None = None,
    context: RankingContext | None = None,
    settings: Settings | None = None,
) -> list[ContactCandidate]:
    """Return meaningful contacts, ordered by priority.

    Returns at least ``contacts_target`` when that many are meaningful, and up
    to ``contacts_max``; weak candidates are never padded in to reach a number.
    """
    settings = settings or get_settings()
    cap = limit if limit is not None else settings.contacts_max

    ranked = rank_contacts(candidates, context)
    meaningful = [c for c in ranked if c.meaningful]

    # Collapse duplicates of the same person discovered on multiple pages.
    deduped: list[ContactCandidate] = []
    seen: set[str] = set()
    for c in meaningful:
        if c.identity_key in seen:
            continue
        seen.add(c.identity_key)
        deduped.append(c)

    return deduped[:cap]


def rank_pocs(contacts: list[dict], domain: str | None, limit: int | None = None) -> list[dict]:
    """Final POC ranking for a lead, after enrichment and before email verification.

    Quality > Quota: only named people whose title maps to a POC tier (P1 Owner /
    CEO / President / Managing Partner, P2 COO / VP Operations / Director of
    Operations / GM, P3 HR / Talent, P4 Plant / Operations Manager) are kept; the
    list is never padded. Order: tier, then email quality (PERSONAL > ROLE >
    DOMAIN_MISMATCH > EXTERNAL_UNVERIFIED > PORTAL_GENERATED > none), then the
    earlier evidence source, then the public rank score. Sets ``poc_rank``.
    """
    pocs = []
    for contact in contacts:
        priority = infer_priority(contact.get("title"))
        if not (contact.get("name") or "").strip() or priority is None:
            continue
        pocs.append({**contact, "title_priority": priority})
    pocs.sort(key=lambda c: (c["title_priority"], -email_strength(c.get("email"), domain),
                             source_rank(c), -(c.get("rank_score") or 0)))
    pocs = pocs[:limit] if limit is not None else pocs
    for rank, contact in enumerate(pocs, 1):
        contact["poc_rank"] = rank
    return pocs


class ContactRanker:
    """Ranks and selects contacts for a company."""

    def __init__(self, settings: Settings | None = None, logger=None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.contacts.ranking")

    def select(
        self,
        candidates: list[ContactCandidate],
        job_titles: list[str | None],
        limit: int | None = None,
    ) -> list[ContactCandidate]:
        context = RankingContext(hiring_priority=dominant_hiring_priority(job_titles))
        selected = select_contacts(
            candidates, limit=limit, context=context, settings=self.settings
        )
        self.log.info(
            "contact_selection",
            candidate_count=len(candidates),
            selected_count=len(selected),
            hiring_priority=context.hiring_priority,
            priorities=[c.priority for c in selected],
            met_target=len(selected) >= self.settings.contacts_target,
        )
        return selected
