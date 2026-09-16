"""Supabase client access (service role).

Uses the official ``supabase-py`` client. The service role key bypasses RLS,
which is required for writing pipeline records.
"""
from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from nexbase.config import get_settings
from nexbase.core.errors import ConfigurationError


@lru_cache
def get_supabase() -> Client:
    """Return a cached Supabase client authenticated with the service role."""
    settings = get_settings()
    if not settings.supabase_configured:
        raise ConfigurationError(
            "Supabase URL and service role key must be set via "
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )
    return create_client(settings.supabase_url, settings.supabase_service_role_key)