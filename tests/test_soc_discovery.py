"""Taxonomy/SOC/NAICS as retained supporting infrastructure.

This file used to assert autonomous SOC-driven discovery: an 80/20 core vs
exploration split, allocation across the industry mix, terms sampled from the
whole occupation universe and locations sampled from a metro list. None of that
is how a run is decided any more - the operator decides - so those assertions
are gone rather than kept alive to protect removed behaviour.

What remains is what the data is still *for*: sector lists, job-title variants,
classification and evidence. There is no sector exclusion list. Deterministic
planning is covered in ``test_user_controlled_discovery.py``.
"""
from __future__ import annotations

import pytest

from nexbase.discovery import taxonomy
from nexbase.pipeline.dedupe import dedupe_jobs
from nexbase.pipeline.freshness import FreshnessFilter
from nexbase.pipeline.normalize import normalize_job
from nexbase.pipeline.qualification import score_company


# ---------------------------------------------------------------------------
# The occupation universe is still loaded from data, not written by hand
# ---------------------------------------------------------------------------
def test_full_soc_universe_is_loaded_not_a_fixed_list():
    terms = taxonomy.load_search_terms()
    assert len(terms) > 10000, "should span the whole occupation universe"
    assert len({t.soc_code for t in terms}) > 700
    assert {t.tier for t in terms} == {taxonomy.CORE, taxonomy.EXPLORATION}


def test_computer_and_mathematical_occupations_are_excluded():
    assert not [t for t in taxonomy.load_search_terms() if t.soc_major == "15-0000"]


def test_core_tier_covers_all_ten_client_industries():
    from nexbase.core.enums import ClientIndustry

    labelled = {t.client_industry for t in taxonomy.load_search_terms() if t.is_core}
    assert labelled == {i.value for i in ClientIndustry}


def test_title_variants_come_from_the_taxonomy():
    terms = taxonomy.suggest_terms_for_sector("Manufacturing", count=12)
    assert len(terms) == 12
    assert len(set(terms)) == 12
    assert all(taxonomy.is_searchable(t) for t in terms)


# ---------------------------------------------------------------------------
# Industry universe and classification
# ---------------------------------------------------------------------------
def test_industry_universe_is_the_full_naics_subsector_set():
    industries = taxonomy.load_industries()
    assert len(industries) >= 80, "universe must not be the ten client industries"
    assert all(i.naics3 and i.title for i in industries)
    assert len({i.naics3 for i in industries}) == len(industries)


def test_universe_spans_core_and_non_core():
    industries = taxonomy.load_industries()
    core = [i for i in industries if i.is_core]
    non_core = [i for i in industries if not i.is_core]
    assert len(core) >= 40
    assert len(non_core) >= 30
    assert not {i.naics3 for i in core} & {i.naics3 for i in non_core}


def test_manufacturing_has_many_subsectors_not_one_bucket():
    mfg = [i for i in taxonomy.load_industries() if i.is_manufacturing]
    assert len(mfg) >= 15
    assert len({i.naics3 for i in mfg}) == len(mfg)


def test_subsector_terms_come_from_its_real_occupations():
    mfg = next(i for i in taxonomy.load_industries() if i.is_manufacturing)
    terms = taxonomy.suggest_terms_for_industry(mfg, 8)
    assert terms
    assert len(terms) <= 8
    assert all(taxonomy.is_searchable(t) for t in terms)


# ---------------------------------------------------------------------------
# SOC 15-0000 (Computer and Mathematical) is excluded in the data itself
# ---------------------------------------------------------------------------
def test_15_0000_remains_excluded_everywhere():
    assert not [t for t in taxonomy.load_search_terms() if t.soc_major == "15-0000"]
    for industry in taxonomy.load_industries():
        assert not [s for s in industry.soc_codes if s.startswith("15-")]


def test_industry_universe_is_not_hardcoded_in_scraper_modules():
    import inspect

    from nexbase.discovery import ats_discovery, board_scrapers, jobspy_discovery

    for module in (board_scrapers, jobspy_discovery, ats_discovery):
        src = inspect.getsource(module)
        assert "naics" not in src.lower()


# ---------------------------------------------------------------------------
# The rest of the pipeline is indifferent to where a job came from
# ---------------------------------------------------------------------------
def test_cross_source_dedup_merges_jobs_from_different_sources(make_job, settings, now):
    jobs = [
        normalize_job(make_job(company="Acme Steel Co", title="Welder",
                               source_site="indeed", external_id="a")),
        normalize_job(make_job(company="Acme Steel Company", title="Machinist",
                               source_site="lever", external_id="b")),
    ]
    aggregates = dedupe_jobs(jobs)
    assert len(aggregates) == 1
    assert aggregates[0].hiring_intensity == 2


def test_freshness_applies_to_jobs_from_any_source(make_job, settings, now):
    jobs = [
        normalize_job(make_job(company="Acme Steel Co", days_old=2, external_id="f")),
        normalize_job(make_job(company="Acme Steel Co", days_old=40, external_id="s")),
    ]
    fresh = FreshnessFilter(settings).filter(dedupe_jobs(jobs), now=now)
    assert len(fresh) == 1
    assert len(fresh[0].fresh_jobs) == 1


def test_search_intent_never_becomes_a_company_industry(make_job, settings, now):
    """The sector searched is intent. It says nothing about the employer."""
    job = make_job(company="Mystery Co", industry=None)
    job.search_industry = "Manufacturing"
    normalized = normalize_job(job)
    assert normalized.company_industry is None
    assert normalized.search_industry == "Manufacturing"

    fresh = FreshnessFilter(settings).filter(dedupe_jobs([normalized]), now=now)[0]
    result = score_company(fresh, settings=settings)
    # The label travels, but only ever as intent: it is not "known", it is
    # sourced as DISCOVERY_INTENT, and the company is still flagged unknown.
    assert result.profile.industry_known is False
    assert result.profile.industry_source == "DISCOVERY_INTENT"
    assert "INDUSTRY_UNKNOWN" in result.review_flags
