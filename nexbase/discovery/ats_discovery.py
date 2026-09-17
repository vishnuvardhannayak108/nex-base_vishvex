"""Module 1: ATS Discovery (ats-scrapers).

High-quality, direct-employer hiring signals from applicant tracking systems
(Greenhouse, Lever, Ashby, Workday, SmartRecruiters, etc.).

``ats_scrapers`` offers two layers and they have wildly different costs:

* ``ats_scrapers.search()`` reads a **hosted static snapshot**. Called without
  an ``ats`` slice it downloads the *full* dataset - 5.2M rows, ~16.9 GB as of
  schema v2.0 - into memory and filters client-side. That is refused here
  unless ``settings.ats_allow_full_snapshot`` is explicitly set.
* Named slices are read through :class:`ATSSliceStore`: downloaded once per
  dataset version to a local cache, and read back with only the columns NexBase
  maps, only US rows, and only rows inside the freshness window.

The dataset also has no ``company_url``/``website`` column, so ATS rows never
carry a real domain. :meth:`resolve_company_sites` fills that gap from the
package's companies directory (80k tenants with careers URLs).
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nexbase.config import Settings, get_settings
from nexbase.core.errors import DiscoveryError
from nexbase.core.geo import _STATE_NAME_OF, US_STATES, in_us_state, us_state_of  # noqa: F401
from nexbase.core.models import RawJob
from nexbase.core.source_tracking import ATS, SourceInfo
from nexbase.core.timeutils import coerce_datetime
from nexbase.logging_setup import get_logger

_ATS_COLUMNS = {
    "external_id": ("global_id", "ats_id", "id"),
    "title": ("title",),
    "company_name": ("company",),
    "location": ("location",),
    "description": ("description",),
    "application_url": ("url",),
    "apply_url": ("apply_url", "url"),
    "ats_platform": ("ats_type", "ats"),
    "country": ("country_iso", "country"),
    "employment_type": ("employment_type",),
}

#: Columns NexBase maps. The rest of a slice (the source-specific ``raw`` JSON,
#: salary and geo detail) is never read.
SLICE_COLUMNS = (
    "global_id", "ats_id", "url", "apply_url", "title", "company", "ats_type",
    "location", "country_iso", "is_remote", "employment_type", "description",
    "posted_at",
)


def _pick(row: dict, keys: tuple[str, ...]):
    for key in keys:
        value = row.get(key)
        if value is not None and value == value and value != "":
            return value
    return None


class ATSSliceStore:
    """ATS slices on local disk, read back filtered to what a US run can use.

    One file per dataset version: the manifest's sha256 changes when a slice is
    regenerated, so an unchanged slice downloads once instead of once per run.
    (That hash and ``size_bytes`` describe the CSV artifact - measured on
    paylocity, 5.6 MB CSV vs 0.7 MB Parquet - so a download is verified by row
    count, not by hash.)

    Reading keeps only US rows and rows posted inside the freshness window. A
    slice is mostly other countries and older postings, and the library's
    ``limit`` truncates before any later filter could skip them. Undated rows
    are kept for the freshness stage to judge.
    """

    def __init__(self, directory, max_age_days: int, logger=None) -> None:
        self.directory = Path(directory).expanduser()
        self.max_age_days = max_age_days
        self.log = logger or get_logger("nexbase.discovery.ats.store")

    def fetch(self, name: str, manifest) -> Path:
        """Local path of the current version of slice ``name``."""
        import httpx
        import pyarrow.parquet as pq

        entry = manifest.by_ats.get(name)
        if entry is None or entry.parquet is None:
            raise DiscoveryError(f"ATS slice {name!r} has no parquet artifact in the manifest")
        version = (entry.sha256 or str(entry.size_bytes))[:16]
        path = self.directory / f"{name}-{version}.parquet"
        if path.exists():
            return path

        self.directory.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        with httpx.stream("GET", str(entry.parquet), follow_redirects=True,
                          timeout=httpx.Timeout(60.0, read=300.0)) as response:
            response.raise_for_status()
            with part.open("wb") as fh:
                for chunk in response.iter_bytes(1 << 20):
                    fh.write(chunk)
        rows = pq.ParquetFile(part).metadata.num_rows
        if rows != entry.rows:
            part.unlink(missing_ok=True)
            raise DiscoveryError(
                f"ATS slice {name!r} downloaded {rows} rows; the manifest lists {entry.rows}")
        part.replace(path)
        for older in self.directory.glob(f"{name}-*.parquet"):
            if older != path:
                older.unlink(missing_ok=True)
        self.log.info("ats_slice_downloaded", ats=name, rows=rows,
                      bytes=path.stat().st_size)
        return path

    def load(self, name: str, manifest, now: datetime | None = None):
        import pandas as pd
        import pyarrow.parquet as pq

        path = self.fetch(name, manifest)
        present = set(pq.read_schema(path).names)
        filters = [("country_iso", "=", "US")] if "country_iso" in present else None
        if filters is None:
            self.log.warning("ats_slice_has_no_country", ats=name)
        frame = pq.read_table(
            path, columns=[c for c in SLICE_COLUMNS if c in present], filters=filters,
        ).to_pandas()
        us_rows = len(frame)
        if "posted_at" in frame:
            # ISO8601, not an inferred format: slices mix "...:19+00:00" and
            # "...:19.378+00:00". Inferred from the first row, the rest became NaT and
            # passed as undated - live 2026-09-17, 75k of 115k SmartRecruiters US rows,
            # up to 1,011 days old.
            posted = pd.to_datetime(frame["posted_at"], utc=True, errors="coerce",
                                    format="ISO8601")
            cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=self.max_age_days)
            frame = frame[posted.isna() | (posted >= cutoff)].reset_index(drop=True)
        self.log.info("ats_slice_read", ats=name, us_rows=us_rows, fresh_or_undated=len(frame))
        return frame



class ATSSliceCache:
    """Holds each ATS slice once, for the lifetime of one discovery run.

    The upstream client re-downloads a per-ATS slice on every call, so a run
    that probes three search terms across ten slices paid for thirty downloads
    of ten files - and the empty-result retry doubled some of them again.

    Downloads are serialised per slice: two probes asking for `greenhouse` at
    the same moment wait on one download rather than starting two. Different
    slices are unaffected and still load concurrently. A loader that raises, or
    returns nothing, leaves the cache empty so the next caller retries.
    """

    def __init__(self, logger=None) -> None:
        self._frames: dict[str, object] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.downloads = 0
        self.hits = 0
        self.log = logger or get_logger("nexbase.discovery.ats.cache")

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, key: str, loader):
        """Return the cached slice, loading it once if this is the first ask."""
        frame = self._frames.get(key)
        if frame is not None:
            self.hits += 1
            return frame

        with self._lock_for(key):
            # Another thread may have loaded it while we waited for the lock.
            frame = self._frames.get(key)
            if frame is not None:
                self.hits += 1
                return frame
            frame = loader()
            if frame is None:
                # Nothing usable came back. Caching that would turn one bad
                # download into a run-long outage for the slice.
                return None
            self._frames[key] = frame
            self.downloads += 1
            self.log.info("ats_slice_loaded", ats=key, rows=len(frame),
                          downloads=self.downloads, hits=self.hits)
            return frame

    @property
    def slices(self) -> int:
        return len(self._frames)

    def stats(self) -> dict:
        return {"slices": self.slices, "downloads": self.downloads,
                "reused": self.hits}


@lru_cache(maxsize=1)
def _caching_client_class():
    """Build the Client subclass once, keeping the ats_scrapers import lazy."""
    from ats_scrapers import Client

    class CachingClient(Client):
        """A Client whose per-ATS slice loads are served from a run cache.

        `load()` is the only override. Everything else - the manifest, the
        companies directory, and `search()`'s filtering - is the library's.
        """

        slice_cache = None
        slice_store = None

        def load(self, *, ats=None, date=None):
            cache = self.slice_cache
            # A dated delta or the full snapshot is not a slice; leave both to
            # the library, which memoises the snapshot itself.
            if cache is None or ats is None or date is not None:
                return Client.load(self, ats=ats, date=date)
            key = str(getattr(ats, "value", ats))
            store = self.slice_store
            loader = ((lambda: store.load(key, self.manifest)) if store is not None
                      else (lambda: Client.load(self, ats=ats)))
            frame = cache.get(key, loader)
            if frame is None:
                return loader()
            return frame

    return CachingClient


def build_cached_client(cache: "ATSSliceCache | None", store: "ATSSliceStore | None" = None):
    """An ats_scrapers Client that reads slices through ``cache`` (and ``store``)."""
    client = _caching_client_class()()
    client.slice_cache = cache
    client.slice_store = store
    return client


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _search_ats(client=None, **kwargs):
    """One dataset search, through ``client`` when the caller supplied one.

    Without a client this falls back to the module-level helper, whose default
    client re-downloads a slice per call - correct, just wasteful.
    """
    if client is not None:
        return client.search(**kwargs)

    import ats_scrapers

    return ats_scrapers.search(**kwargs)



def ats_location_filter(location: str | None) -> str | None:
    """Narrow a planner location to what the ATS dataset can actually match.

    The dataset stores locations inconsistently - "Mason, OH", "Austin, TX",
    "Warner Robins, Georgia, United States" - and the library filters by
    substring. Measured on the greenhouse slice for "machinist":

        no location    87 jobs
        "OH"            5 jobs
        "Ohio"          0 jobs
        "Columbus, OH"  0 jobs
        "Columbus"      0 jobs

    So passing the planner's "City, ST" all but guarantees an empty result: it
    demands that exact city string. The state abbreviation is the widest filter
    the data reliably supports; city precision is applied downstream, where the
    location is normalised anyway.

    Because the code is matched as a substring, everything it returns is
    re-checked with :func:`in_us_state`.
    """
    return us_state_of(location)

class ATSDiscovery:
    """Discovers raw jobs from ATS platforms via ats-scrapers."""

    source: SourceInfo = ATS

    def __init__(self, settings: Settings | None = None, logger=None,
                 slice_cache: "ATSSliceCache | None" = None) -> None:
        self.settings = settings or get_settings()
        self.log = logger or get_logger("nexbase.discovery.ats")
        self._client = None
        # The runner hands every probe in a run the same cache, so a slice is
        # downloaded once per run rather than once per probe. A standalone
        # instance gets its own, which still collapses repeats within one call.
        self.slice_cache = slice_cache if slice_cache is not None else ATSSliceCache(self.log)
        self.slice_store = ATSSliceStore(
            self.settings.ats_cache_dir, self.settings.freshness_max_days, self.log)

    # ------------------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            self._client = build_cached_client(self.slice_cache, self.slice_store)
        return self._client

    # ------------------------------------------------------------------
    def search(
        self,
        query: str | None = None,
        location: str | None = None,
        company: str | None = None,
        ats: str | list[str] | None = None,
        remote: bool | None = None,
        limit: int | None = 500,
        client_industry: str | None = None,
    ) -> list[RawJob]:
        """Search one or more ATS dataset slices.

        ``ats=None`` means "the entire 16.9 GB snapshot" in the underlying
        library, so it is rejected unless explicitly enabled in settings.
        """
        slices = self._resolve_slices(ats)
        # The dataset matches the location by naive substring, so "GA" also
        # matches "Garden Grove, CA" and "CO" matches "Costa Mesa". The code is
        # still the widest filter the data supports, so it is used to keep the
        # scan cheap and every row is verified against it below.
        state = ats_location_filter(location)
        location = state or location

        # One client for the whole search, so every slice it loads goes
        # through the same run cache. If ats_scrapers cannot be constructed we
        # fall back to the module-level helper rather than failing the search.
        try:
            client = self._get_client()
        except Exception as exc:
            self.log.warning("ats_client_unavailable", error=str(exc))
            client = None

        records: list[RawJob] = []
        #: Per-slice failures from the last search. A failed slice is skipped,
        #: not fatal, so the caller needs this to tell failure from "empty".
        self.errors: list[str] = []
        dropped_out_of_state = 0
        for slice_name in slices:
            self.log.info(
                "ats_search_start",
                query=query,
                location=location,
                company=company,
                ats=slice_name,
            )
            try:
                df = _search_ats(
                    client=client,
                    query=query,
                    location=location,
                    company=company,
                    ats=slice_name,
                    remote=remote,
                    limit=limit,
                )
            except Exception as exc:
                self.log.error("ats_search_error", ats=slice_name, error=str(exc))
                self.errors.append(f"{slice_name}: {exc}")
                continue

            if (df is None or df.empty) and state:
                # Slices disagree on how they store a location, and the filter
                # is case-sensitive. Measured for "machinist": greenhouse gives
                # 5 rows for "OH" and 0 for "Ohio"; workable gives 0 and 1. So
                # an empty result is retried with the other spelling before the
                # slice is written off.
                alias = _STATE_NAME_OF.get(state)
                if alias:
                    self.log.info("ats_retry_state_name", ats=slice_name,
                                  state=state, alias=alias)
                    try:
                        df = _search_ats(client=client, query=query,
                                         location=alias, company=company,
                                         ats=slice_name, remote=remote,
                                         limit=limit)
                    except Exception as exc:
                        self.log.error("ats_search_error", ats=slice_name,
                                       error=str(exc))
                        self.errors.append(f"{slice_name}: {exc}")
                        continue

            if df is None or df.empty:
                self.log.info("ats_search_empty", ats=slice_name)
                continue

            found = [self._to_raw_job(row, client_industry=client_industry)
                     for row in df.to_dict("records")]
            if state:
                kept = [job for job in found if in_us_state(job.location, state)]
                dropped_out_of_state += len(found) - len(kept)
                found = kept
            records.extend(found)
            self.log.info("ats_search_complete", ats=slice_name,
                          count=len(found), scanned=len(df))

        if dropped_out_of_state:
            self.log.info("ats_out_of_state_dropped", state=state,
                          count=dropped_out_of_state)
        self.log.info("ats_total", count=len(records),
                      slice_cache=self.slice_cache.stats())
        return records

    def _resolve_slices(self, ats: str | list[str] | None) -> list[str | None]:
        if ats is None:
            if self.settings.ats_allow_full_snapshot:
                self.log.warning("ats_full_snapshot_enabled", note="~16.9GB download")
                return [None]
            raise DiscoveryError(
                "ATS search requires an `ats` slice. Searching the full "
                "snapshot downloads ~16.9 GB; set ATS_ALLOW_FULL_SNAPSHOT=true "
                "to opt in."
            )
        if isinstance(ats, str):
            return [ats]
        return list(ats)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    #: A directory row must look this much like the company we asked about
    #: before its URL is trusted. Below it, no domain is better than a wrong one.
    MIN_NAME_SIMILARITY = 0.75

    def resolve_company_sites(
        self, company_names: list[str], locations: dict[str, str] | None = None
    ) -> dict[str, dict]:
        """Map company name -> a verified directory match.

        The jobs dataset has no website column, so this is how an ATS-sourced
        company acquires a resolvable URL. The directory search is fuzzy, so
        taking its first hit unverified attached other companies' domains -
        "Summit Construction" matching a same-named firm two states away. Each
        candidate is now scored on name overlap, and on location when the
        directory publishes one, before its URL is accepted.

        Returns ``{name: {"url", "matched_name", "similarity", "location_match"}}``
        so the caller can record why the match was believed.
        """
        resolved: dict[str, dict] = {}
        try:
            client = self._get_client()
        except Exception as exc:
            self.log.warning("ats_companies_unavailable", error=str(exc))
            return resolved

        from nexbase.pipeline.domain_resolver import location_tokens, name_similarity

        for name in company_names:
            if not name:
                continue
            try:
                matches = client.find_company(name, limit=5)
            except Exception as exc:
                self.log.debug("ats_find_company_error", company=name, error=str(exc))
                continue
            if matches is None or matches.empty:
                continue

            wanted = location_tokens((locations or {}).get(name))
            viable = []
            for _, candidate in matches.iterrows():
                row = candidate.to_dict()
                url = row.get("url")
                if not url:
                    continue
                matched_name = str(row.get("name") or row.get("company") or "")
                score = name_similarity(name, matched_name)
                if score < self.MIN_NAME_SIMILARITY:
                    continue
                where = " ".join(
                    str(row.get(k) or "")
                    for k in ("location", "city", "state", "country")
                ).strip()
                viable.append({
                    "url": str(url), "matched_name": matched_name,
                    "similarity": score,
                    "location_match": bool(wanted & location_tokens(where))
                    if (wanted and where) else False,
                    "has_location": bool(where),
                    # The jobs dataset stores the ATS tenant slug in `company`,
                    # and the directory publishes that same slug. Equality is
                    # an identifier match, not a fuzzy name guess.
                    "exact_slug": name.strip().lower()
                    == str(row.get("slug") or "").strip().lower(),
                })

            verdict = self._decide_match(name, viable, bool(wanted))
            resolved[name] = verdict
            self.log.debug("ats_company_match", company=name, **verdict)
        matched = sum(1 for v in resolved.values() if v["status"] == "MATCHED")
        ambiguous = sum(1 for v in resolved.values() if v["status"] == "AMBIGUOUS")
        self.log.info(
            "ats_company_sites_resolved", requested=len(company_names),
            matched=matched, ambiguous=ambiguous,
        )
        return resolved

    def _decide_match(self, name: str, viable: list[dict], had_location: bool) -> dict:
        """Accept, refuse, or declare the match ambiguous.

        A wrong domain is worse than no domain: it sends contact discovery to
        another company's website and attributes their staff to this employer.
        So anything short of an unambiguous match returns AMBIGUOUS and is left
        for a human.
        """
        if not viable:
            return {"status": "NO_MATCH", "url": None, "matched_name": None,
                    "similarity": 0.0, "location_match": False,
                    "reason": "no candidate cleared the name threshold"}

        # An exact tenant-slug match identifies the company outright, so it is
        # settled before any location reasoning: the directory's own location
        # column is sparse and often disagrees with where the job is.
        exact = [v for v in viable if v.get("exact_slug")]
        if len(exact) == 1:
            return {**exact[0], "status": "MATCHED", "confidence": "HIGH",
                    "reason": "ats tenant slug matches the directory entry"}

        corroborated = [v for v in viable if v["location_match"]]
        if len(corroborated) == 1:
            return {**corroborated[0], "status": "MATCHED", "confidence": "HIGH",
                    "reason": "name and location agree"}
        if len(corroborated) > 1:
            return {"status": "AMBIGUOUS", "url": None,
                    "matched_name": None, "similarity": 0.0, "location_match": True,
                    "reason": f"{len(corroborated)} candidates match name and location"}

        # Nothing corroborated. If the posting told us where the company is and
        # a candidate published a location, they disagree - that is a conflict,
        # not a near miss.
        if had_location and any(v["has_location"] for v in viable):
            return {"status": "AMBIGUOUS", "url": None, "matched_name": None,
                    "similarity": 0.0, "location_match": False,
                    "reason": "candidate locations contradict the posting"}

        if len(viable) == 1:
            # Name alone, with nothing to corroborate it. The directory search
            # is fuzzy, so this is a candidate for review rather than a fact.
            return {**viable[0], "status": "AMBIGUOUS", "confidence": "MEDIUM",
                    "url": None, "candidate_url": viable[0]["url"],
                    "reason": "single name match, nothing corroborates it"}

        return {"status": "AMBIGUOUS", "url": None, "matched_name": None,
                "similarity": 0.0, "location_match": False,
                "reason": f"{len(viable)} equally plausible candidates"}

    # ------------------------------------------------------------------
    def _to_raw_job(self, row: dict, client_industry: str | None = None) -> RawJob:
        """Map one ATS row.

        ``client_industry`` is the industry of the QUERY that found this row, so
        it is recorded as ``search_industry`` (discovery intent) and never as
        ``company_industry`` - the ATS dataset says nothing about the employer's
        sector.
        """
        return RawJob(
            search_industry=client_industry,
            source_type=self.source.source_type.value,
            source_priority=int(self.source.source_priority.value),
            source_site=_pick(row, ("ats_type", "ats")),
            external_id=_pick(row, _ATS_COLUMNS["external_id"]),
            title=_pick(row, _ATS_COLUMNS["title"]),
            company_name=_pick(row, _ATS_COLUMNS["company_name"]),
            # The ATS dataset has no employer-website column at all.
            company_url=None,
            company_website=None,
            location=_pick(row, _ATS_COLUMNS["location"]),
            description=_pick(row, _ATS_COLUMNS["description"]),
            posted_at=coerce_datetime(row.get("posted_at")),
            application_url=_pick(row, _ATS_COLUMNS["application_url"]),
            apply_url=_pick(row, _ATS_COLUMNS["apply_url"]),
            ats_platform=_pick(row, _ATS_COLUMNS["ats_platform"]),
            country=_pick(row, _ATS_COLUMNS["country"]),
            employment_type=_pick(row, _ATS_COLUMNS["employment_type"]),
            is_remote=row.get("is_remote"),
            company_industry=None,
            company_employee_count=None,
            raw={k: v for k, v in row.items() if k != "description"},
        )
