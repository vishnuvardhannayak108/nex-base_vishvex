"""Repository: single place that talks to Supabase.

All writes flow through here. Two failure modes are deliberately distinct:

* **Not configured** (no URL/key) -> methods no-op and return ``None``. The
  pipeline stays runnable and testable offline.
* **Configured but the write failed** -> raises :class:`DatabaseError` when
  ``settings.db_strict`` is on (the default). Silently swallowing these is how
  an unapplied schema masquerades as a successful run.
"""
from __future__ import annotations

from typing import Any, Iterable

from nexbase.config import Settings, get_settings
from nexbase.core.errors import DatabaseError
from nexbase.logging_setup import get_logger


class SupabaseRepository:
    """Persists pipeline records to Supabase (or no-ops without credentials)."""

    def __init__(self, client=None, logger=None, settings: Settings | None = None) -> None:
        self.log = logger or get_logger("nexbase.db.repository")
        self.settings = settings or get_settings()
        self.strict = self.settings.db_strict
        if client is not None:
            self._client = client
        elif self.settings.supabase_configured:
            from nexbase.db.supabase_client import get_supabase

            self._client = get_supabase()
        else:
            self._client = None

    @property
    def configured(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # Companies
    # ------------------------------------------------------------------
    def upsert_company(
        self,
        data: dict[str, Any],
        on_conflict: str = "normalized_domain,normalized_name",
    ) -> str | None:
        return self._execute("companies", "upsert", data, on_conflict=on_conflict)

    def update_company(self, company_id: str | None, data: dict[str, Any]) -> None:
        if company_id:
            self._update("companies", {"id": company_id}, data)

    def find_company(self, normalized_domain: str, normalized_name: str) -> dict[str, Any] | None:
        rows = self.select(
            "companies",
            "*",
            filters={"normalized_domain": normalized_domain, "normalized_name": normalized_name},
            limit=1,
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Jobs / contacts / evidence
    # ------------------------------------------------------------------
    def upsert_job(self, data: dict[str, Any]) -> str | None:
        return self._execute(
            "jobs", "upsert", data, on_conflict="company_id,source_type,external_id"
        )

    def upsert_contact(self, data: dict[str, Any]) -> str | None:
        return self._execute(
            "contacts", "upsert", data, on_conflict="company_id,name,title"
        )

    def update_contact(self, contact_id: str | None, data: dict[str, Any]) -> None:
        if contact_id:
            self._update("contacts", {"id": contact_id}, data)

    def insert_evidence(self, data: dict[str, Any]) -> str | None:
        return self._execute("evidence", "insert", data)

    def insert_qualification_reason(self, data: dict[str, Any]) -> str | None:
        return self._execute("qualification_reasons", "insert", data)

    def insert_enrichment_log(self, data: dict[str, Any]) -> str | None:
        return self._execute("enrichment_logs", "insert", data)

    def insert_email_verification(self, data: dict[str, Any]) -> str | None:
        return self._execute("email_verification_logs", "insert", data)

    # ------------------------------------------------------------------
    # Discovery state / history
    # ------------------------------------------------------------------
    def insert_discovery_run(self, data: dict[str, Any]) -> str | None:
        return self._execute("discovery_runs", "insert", data)

    def update_discovery_run(self, run_id: str | None, data: dict[str, Any]) -> None:
        if run_id:
            self._update("discovery_runs", {"id": run_id}, data)

    def upsert_hiring_history(self, data: dict[str, Any]) -> str | None:
        return self._execute(
            "company_hiring_history", "upsert", data, on_conflict="company_id,observed_on"
        )

    def count_hiring_history(self, company_id: str) -> int:
        if not self.configured:
            return 0
        try:
            resp = (
                self._client.table("company_hiring_history")
                .select("id", count="exact")
                .eq("company_id", company_id)
                .execute()
            )
            return int(getattr(resp, "count", 0) or 0)
        except Exception as exc:
            self._fail("company_hiring_history", "count", exc)
            return 0

    def insert_audit(self, data: dict[str, Any]) -> str | None:
        # Audit writes must never take the pipeline down.
        return self._execute("audit_logs", "insert", data, strict=False)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------
    def select(
        self,
        table: str,
        columns: str = "*",
        limit: int | None = None,
        filters: dict[str, Any] | None = None,
        order_by: str | None = None,
        descending: bool = True,
        strict: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.configured:
            return []
        try:
            query = self._client.table(table).select(columns)
            for key, value in (filters or {}).items():
                query = query.eq(key, value)
            if order_by:
                query = query.order(order_by, desc=descending)
            if limit is not None:
                query = query.limit(limit)
            response = query.execute()
            return response.data or []
        except Exception as exc:
            self.log.error("db_read_error", table=table, error=str(exc))
            if strict:
                raise DatabaseError(f"read on '{table}' failed: {exc}") from exc
            return []

    def health_check(self) -> dict[str, Any]:
        """Verify every expected table is reachable. Used by /health and the CLI."""
        tables = [
            "companies", "jobs", "contacts", "evidence", "qualification_reasons",
            "enrichment_logs", "email_verification_logs", "discovery_runs",
            "company_hiring_history", "audit_logs",
        ]
        if not self.configured:
            return {"configured": False, "schema_applied": False, "missing": tables}

        missing: list[str] = []
        for table in tables:
            try:
                self._client.table(table).select("id").limit(1).execute()
            except Exception:
                missing.append(table)
        return {
            "configured": True,
            "schema_applied": not missing,
            "missing": missing,
        }

    # ------------------------------------------------------------------
    def _execute(
        self,
        table: str,
        mode: str,
        data: dict[str, Any],
        on_conflict: str | None = None,
        strict: bool | None = None,
    ) -> str | None:
        if not self.configured:
            self.log.debug("db_write_dry_run", table=table, mode=mode)
            return None

        payload = _strip_nones_for_upsert(data) if mode == "upsert" else data
        try:
            if mode == "upsert":
                response = (
                    self._client.table(table)
                    .upsert(payload, on_conflict=on_conflict)
                    .execute()
                )
            else:
                response = self._client.table(table).insert(payload).execute()
            rows = response.data if getattr(response, "data", None) else []
            return rows[0].get("id") if rows else None
        except Exception as exc:
            self._fail(table, mode, exc, strict=strict)
            return None

    def _update(self, table: str, filters: dict[str, Any], data: dict[str, Any]) -> None:
        if not self.configured:
            self.log.debug("db_update_dry_run", table=table)
            return
        try:
            query = self._client.table(table).update(data)
            for key, value in filters.items():
                query = query.eq(key, value)
            query.execute()
        except Exception as exc:
            self._fail(table, "update", exc)

    def _fail(self, table: str, mode: str, exc: Exception, strict: bool | None = None) -> None:
        strict = self.strict if strict is None else strict
        self.log.error("db_write_error", table=table, mode=mode, error=str(exc))
        if strict:
            raise DatabaseError(
                f"{mode} on '{table}' failed: {exc}. "
                "If the table does not exist, apply the schema with "
                "`python -m nexbase.cli db apply-schema`."
            ) from exc


def _strip_nones_for_upsert(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so an upsert never overwrites a known fact with null.

    Discovery is incremental: a later, thinner sighting of a company must not
    erase an industry or employee range an earlier, richer one established.
    """
    return {k: v for k, v in data.items() if v is not None}


class InertRepository:
    """No-op repository used for dry runs and unit tests."""

    configured = False
    strict = False

    def __getattr__(self, name: str):
        if name.startswith(("insert_", "upsert_", "update_")):
            return lambda *a, **k: None
        if name.startswith(("list_", "select")):
            return lambda *a, **k: []
        if name.startswith(("count", "find_", "get_")):
            return lambda *a, **k: 0 if name.startswith("count") else None
        raise AttributeError(name)

    def health_check(self) -> dict[str, Any]:
        return {"configured": False, "schema_applied": False, "missing": []}
