"""Discovery taxonomy: client sectors and job-title variants.

The brief says "do not restrict discovery to a fixed job-title list" and
"search both blue-collar and white-collar hiring". This module satisfies both
by deriving the title universe from the BLS/O*NET occupation data already in
the repository (6k+ real reported job titles across the ten client industries)
instead of a hand-written list.

The packaged CSV is produced by ``scripts/build_taxonomy.py``.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from nexbase.core.enums import ClientIndustry

_DATA_PACKAGE = "nexbase.discovery.data"
_SEARCH_TERMS_FILE = "search_terms.csv"


#: SOC major groups never searched (client instruction).
EXCLUDED_SOC_MAJOR_GROUPS: frozenset[str] = frozenset({"15-0000"})

CORE, EXPLORATION = "CORE", "EXPLORATION"


@dataclass(frozen=True)
class SearchTerm:
    tier: str
    client_industry: str
    soc_code: str
    soc_major: str
    soc_title: str
    term: str
    rank: int

    @property
    def is_core(self) -> bool:
        return self.tier == CORE


@lru_cache(maxsize=1)
def load_search_terms() -> tuple[SearchTerm, ...]:
    """Load the packaged occupation-derived search-term universe."""
    path = resources.files(_DATA_PACKAGE).joinpath(_SEARCH_TERMS_FILE)
    with path.open("r", encoding="utf-8", newline="") as fh:
        return tuple(
            SearchTerm(
                tier=row["tier"],
                client_industry=row["client_industry"],
                soc_code=row["soc_code"],
                soc_major=row["soc_major"],
                soc_title=row["soc_title"],
                term=row["search_term"],
                rank=int(row["rank"]),
            )
            for row in csv.DictReader(fh)
            if row["soc_major"] not in EXCLUDED_SOC_MAJOR_GROUPS
        )


@lru_cache(maxsize=1)
def terms_by_industry() -> dict[str, tuple[SearchTerm, ...]]:
    """CORE terms only, grouped by the client industry they belong to."""
    grouped: dict[str, list[SearchTerm]] = {}
    for term in load_search_terms():
        if term.is_core and term.client_industry:
            grouped.setdefault(term.client_industry, []).append(term)
    return {k: tuple(sorted(v, key=lambda t: t.rank)) for k, v in grouped.items()}


def industries() -> list[str]:
    return sorted(terms_by_industry())


#: Rank 0 is the canonical BLS occupation label, which is a poor job-board
#: query: employers advertise "Order Picker", not "Stockers and Order Fillers".
#: Ranks 1-4 are worker-reported titles and search far better.
PREFERRED_RANK_MAX = 5


def is_searchable(term: str) -> bool:
    """Reject phrasings that read as taxonomy labels rather than job ads."""
    if not term or len(term) < 4:
        return False
    lowered = term.lower()
    if "," in term:
        return False               # "Packers and Packagers, Hand"
    if " and " in lowered or "/" in term:
        return False               # "Conveyor Operators and Tenders"
    if lowered.startswith(("all other", "other ")) or lowered.endswith(" all other"):
        return False
    if "except" in lowered or "miscellaneous" in lowered:
        return False
    words = term.split()
    if not 1 <= len(words) <= 4:
        return False
    # Plural head noun is a BLS label tell ("Aerospace Engineers").
    return not (words[-1].endswith("s") and not words[-1].endswith(("ss", "us", "is")))


def _singular(word: str) -> str:
    """Crude but sufficient de-pluralisation for occupation titles."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "ches", "shes", "xes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _title_key(title: str) -> str:
    """Normalise a title for lookup: lowercase, de-pluralised, whitespace-collapsed.

    O*NET publishes occupation titles in the plural ("Carpenters"), while job
    postings advertise the singular ("Carpenter"). Without this both directions
    of that mismatch silently fail to classify.
    """
    words = title.strip().lower().split()
    return " ".join(_singular(w) for w in words)


def industry_for_title(title: str | None) -> str | None:
    """Best-effort reverse lookup: which client industry does this role imply?"""
    if not title:
        return None
    needle = _title_key(title)
    if not needle:
        return None

    index = _exact_term_index()
    exact = index.get(needle)
    if exact:
        return exact

    # Longest matching multi-word term wins, so "operations manager" beats
    # "manager" and a stray short term cannot hijack a specific title.
    best: tuple[int, str] | None = None
    for term, industry in index.items():
        if len(term) >= 8 and term in needle:
            if best is None or len(term) > best[0]:
                best = (len(term), industry)
    return best[1] if best else None


@lru_cache(maxsize=1)
def _exact_term_index() -> dict[str, str]:
    """Map normalised term -> client industry, keyed on the de-pluralised form.

    CORE terms only: an EXPLORATION term has no client industry, and indexing
    it would map real titles to an empty string.
    """
    index: dict[str, str] = {}
    for term in load_search_terms():
        if term.is_core and term.client_industry:
            index.setdefault(_title_key(term.term), term.client_industry)
    return index


# ---------------------------------------------------------------------------
# Industry universe (BLS OEWS NAICS 3-digit x occupation)
# ---------------------------------------------------------------------------
_INDUSTRY_FILE = "industry_occupations.csv"


@dataclass(frozen=True)
class Industry:
    """One NAICS 3-digit subsector and the occupations that staff it."""

    naics3: str
    title: str
    tier: str                 # CORE (a client usual industry) | NON_CORE
    client_industry: str      # populated for CORE only
    soc_codes: tuple[str, ...]

    @property
    def is_core(self) -> bool:
        return self.tier == CORE

    @property
    def is_manufacturing(self) -> bool:
        return self.naics3[:2] in ("31", "32", "33")


@lru_cache(maxsize=1)
def load_industries() -> tuple[Industry, ...]:
    """Every NAICS 3-digit subsector BLS publishes, with its occupations."""
    path = resources.files(_DATA_PACKAGE).joinpath(_INDUSTRY_FILE)
    grouped: dict[str, dict] = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            entry = grouped.setdefault(row["naics3"], {
                "title": row["naics_title"], "tier": row["tier"],
                "client_industry": row["client_industry"], "socs": [],
            })
            entry["socs"].append(row["soc_code"])
    return tuple(
        Industry(naics3=code, title=v["title"], tier=v["tier"],
                 client_industry=v["client_industry"], soc_codes=tuple(v["socs"]))
        for code, v in sorted(grouped.items())
    )


def soc_major(soc_code: str) -> str:
    """The SOC major group a detailed code belongs to: 53-7065 -> 53-0000."""
    code = (soc_code or "").strip()
    return f"{code[:2]}-0000" if len(code) >= 2 else ""


@lru_cache(maxsize=1)
def _terms_by_soc() -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for term in load_search_terms():
        if is_searchable(term.term) and term.rank <= PREFERRED_RANK_MAX:
            grouped.setdefault(term.soc_code, []).append(term.term)
    return {k: tuple(v) for k, v in grouped.items()}


def suggest_terms_for_industry(
    industry: Industry,
    count: int | None = None,
    exclude_majors: frozenset[str] | None = None,
) -> list[str]:
    """Title variants for the occupations that actually staff this industry.

    Occupations come from the BLS employment crosswalk, so the suggestions are
    roles the subsector really hires rather than generic keywords.

    These are **suggestions for the operator**, offered in a dropdown and never
    applied on their own. Discovery searches the terms the operator chose; this
    list neither widens nor narrows that choice, so it can never become a fixed
    title universe. Ordering is deterministic (crosswalk order, then rank), so
    the same industry always offers the same shortlist.

    ``EXCLUDED_SOC_MAJOR_GROUPS`` is always applied on top of
    ``exclude_majors``, so 15-0000 stays excluded.
    """
    by_soc = _terms_by_soc()
    excluded = (exclude_majors or frozenset()) | EXCLUDED_SOC_MAJOR_GROUPS
    pool: list[str] = []
    seen: set[str] = set()
    for soc in industry.soc_codes:
        if soc_major(soc) in excluded:
            continue
        for term in by_soc.get(soc, ()):
            if term not in seen:
                seen.add(term)
                pool.append(term)
    return pool if count is None else pool[:max(0, count)]


def suggest_terms_for_sector(
    sector: str,
    count: int | None = None,
    exclude_majors: frozenset[str] | None = None,
) -> list[str]:
    """Suggested search terms for a client sector label, for the UI dropdown.

    Suggestions only: what actually gets searched is whatever the operator
    keeps or types.
    """
    suggestions: list[str] = []
    seen: set[str] = set()
    for industry in load_industries():
        if industry.client_industry != sector:
            continue
        for term in suggest_terms_for_industry(
                industry, exclude_majors=exclude_majors):
            if term not in seen:
                seen.add(term)
                suggestions.append(term)
    return suggestions if count is None else suggestions[:max(0, count)]


__all__ = [
    "ClientIndustry",
    "Industry",
    "SearchTerm",
    "industries",
    "industry_for_title",
    "load_industries",
    "load_search_terms",
    "suggest_terms_for_industry",
    "suggest_terms_for_sector",
    "terms_by_industry",
]
