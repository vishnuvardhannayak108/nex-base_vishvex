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
import re
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


# ---------------------------------------------------------------------------
# A source's industry label -> NexBase sector (official NAICS titles)
# ---------------------------------------------------------------------------
_NAICS_TITLES_FILE = "naics_titles.csv"

#: Connective words that name no industry.
_LABEL_STOPWORDS = frozenset({"and", "or", "the", "of", "for", "in", "on", "other",
                              "all", "except", "related"})


def _label_words(text: str) -> frozenset[str]:
    text = re.sub(r"\([^)]*\)", " ", text.lower())   # "(except Casino Hotels)"
    return frozenset(_singular(w) for w in re.findall(r"[a-z]+", text)
                     if w not in _LABEL_STOPWORDS)


@lru_cache(maxsize=1)
def _industry_vocabulary() -> tuple[tuple[frozenset[str], str | None, str | None], ...]:
    """``(title words, NexBase sector or None, NAICS code)`` for NAICS titles and sector names."""
    entries = [(_label_words(sector.value), sector.value, None) for sector in ClientIndustry]
    path = resources.files(_DATA_PACKAGE).joinpath(_NAICS_TITLES_FILE)
    with path.open("r", encoding="utf-8", newline="") as fh:
        entries.extend((_label_words(row["naics_title"]), row["client_industry"] or None,
                        row["naics_code"]) for row in csv.DictReader(fh))
    return tuple(entry for entry in entries if entry[0])


def _naics_contains(parent: str | None, child: str | None) -> bool:
    """True when NAICS ``child`` sits under ``parent`` ("48-49" contains "493")."""
    if not parent or not child or parent == child or not child.isdigit():
        return False
    first, _, last = parent.partition("-")
    if last:
        return first <= child[:2] <= last
    return child.startswith(parent)


def sector_for_industry_label(label: str | None) -> str | None:
    """The NexBase sector a source's industry label names, or None.

    A label that is a NexBase sector name is that sector. Otherwise it is
    compared with every official 2022 NAICS title (2 to 6 digits) and the sector
    names: the best matches share the most words with the label, then have the
    fewest words the label lacks, and a NAICS code tied with its own sub-code
    gives way to it. A sector is returned only when every best match maps to
    that one sector (``naics_titles.csv``, built from ``naics_to_industry.csv``)
    and the match covers at least half the label. A best match in a non-NexBase
    industry, or two sectors tied, returns None: "Automotive Dealers" is Retail
    (NAICS 441), never Manufacturing.
    """
    words = _label_words(label or "")
    if not words:
        return None
    for sector in ClientIndustry:
        if words == _label_words(sector.value):
            return sector.value

    best, tied = None, []
    for title, sector, code in _industry_vocabulary():
        shared = len(words & title)
        if not shared:
            continue
        rank = (shared, -len(words | title))
        if best is None or rank > best:
            best, tied = rank, [(sector, code)]
        elif rank == best:
            tied.append((sector, code))
    if best is None or best[0] * 2 < len(words):
        return None
    sectors = {sector for sector, code in tied
               if not any(_naics_contains(code, other) for _, other in tied)}
    return sectors.pop() if len(sectors) == 1 else None


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
