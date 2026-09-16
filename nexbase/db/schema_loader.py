"""Load and apply the canonical schema (schema.sql)."""
from __future__ import annotations

import socket
from importlib import resources
from urllib.parse import urlsplit

_SCHEMA_FILE = "schema.sql"

#: Supabase regions that host the shared connection pooler.
_POOLER_REGIONS = (
    "ap-south-1", "ap-southeast-1", "ap-northeast-1", "ap-northeast-2",
    "ap-southeast-2", "us-east-1", "us-east-2", "us-west-1", "eu-central-1",
    "eu-west-1", "eu-west-2", "eu-west-3", "sa-east-1", "ca-central-1",
)


def load_schema_sql() -> str:
    """Return the full canonical DDL text."""
    return resources.files("nexbase.db").joinpath(_SCHEMA_FILE).read_text(encoding="utf-8")


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


def pooler_candidates(dsn: str) -> list[str]:
    """Derive session-pooler DSNs from a direct-connection DSN.

    Supabase no longer publishes an IPv4 record for ``db.<ref>.supabase.co`` on
    many projects; connections go through a regional pooler instead, with the
    username rewritten to ``postgres.<ref>``. The region is not encoded
    anywhere in the project URL, so each is tried in turn.
    """
    from urllib.parse import quote

    parts = urlsplit(dsn)
    host = parts.hostname or ""
    if not host.startswith("db.") or not parts.password:
        return []
    ref = host.split(".")[1]
    password = quote(parts.password, safe="")
    return [
        f"postgresql://postgres.{ref}:{password}@aws-0-{region}.pooler.supabase.com:5432/postgres"
        for region in _POOLER_REGIONS
    ]


async def resolve_dsn(dsn: str) -> str:
    """Return a DSN that actually connects, falling back to the pooler."""
    import asyncpg

    host = urlsplit(dsn).hostname or ""
    if host and _resolves(host):
        return dsn

    for candidate in pooler_candidates(dsn):
        candidate_host = urlsplit(candidate).hostname or ""
        if not _resolves(candidate_host):
            continue
        try:
            conn = await asyncpg.connect(candidate, ssl="require", timeout=10)
        except Exception:
            continue
        await conn.close()
        return candidate

    raise ConnectionError(
        f"Cannot reach the database host '{host}'. Supabase projects now "
        "connect through a regional pooler; copy the 'Session pooler' URI from "
        "Project Settings > Database and set it as SUPABASE_DB_URL. It looks "
        "like postgresql://postgres.<ref>:<password>@aws-0-<region>."
        "pooler.supabase.com:5432/postgres"
    )


async def apply_schema(dsn: str | None = None) -> None:
    """Apply the canonical schema to a Supabase Postgres database.

    Idempotent: every statement is ``CREATE ... IF NOT EXISTS`` or a trigger
    replace, so re-running is safe and never drops data.
    """
    from nexbase.config import get_settings
    from nexbase.core.errors import ConfigurationError

    dsn = dsn or get_settings().supabase_db_url
    if not dsn:
        raise ConfigurationError("Cannot apply schema: SUPABASE_DB_URL is not set.")

    import asyncpg

    dsn = await resolve_dsn(dsn)
    conn = await asyncpg.connect(dsn, ssl="require")
    try:
        await conn.execute(load_schema_sql())
    finally:
        await conn.close()
