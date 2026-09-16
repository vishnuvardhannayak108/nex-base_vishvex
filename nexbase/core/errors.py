"""Pipeline-specific exception hierarchy."""
from __future__ import annotations


class NexBaseError(Exception):
    """Base exception for all pipeline errors."""


class ConfigurationError(NexBaseError):
    """Required configuration (secrets, URLs) is missing."""


class DatabaseError(NexBaseError):
    """A database read/write failed while Supabase was configured."""


class DiscoveryError(NexBaseError):
    """A discovery source failed or was called with unsafe parameters."""
