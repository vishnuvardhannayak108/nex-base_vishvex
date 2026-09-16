"""Pre-flight validation for every URL the system fetches itself.

The pipeline fetches URLs it did not choose: a ``company_website`` scraped out
of a job posting, a host pulled from a search-results page, a link found in a
sitemap. Anyone who can publish a job posting can therefore aim a server-side
request wherever they like, including at loopback, private ranges and the cloud
metadata endpoint - and the response is persisted as evidence.

So the rule is simple and deliberately strict: http(s) only, no credentials in
the URL, and the host must resolve entirely to public addresses. Rejection is
never a guess about intent; it is a refusal to make the request at all.

``check_url`` is called before the first request and again for each redirect
destination, because a public host can redirect to 127.0.0.1.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hostnames that never need to be resolved to be refused.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
        # Cloud instance metadata, by name.
        "metadata", "metadata.google.internal", "metadata.goog",
        "instance-data",
    }
)

#: Suffixes that only ever name internal hosts.
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain")


class UrlRejected(ValueError):
    """Raised when a URL must not be fetched."""


class HostUnresolved(UrlRejected):
    """The hostname does not resolve.

    A subclass of :class:`UrlRejected` so every caller still refuses the fetch,
    but distinguishable so a dead website is not reported as a security block.
    """


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # `is_global` already excludes private, loopback, link-local (including
    # 169.254.169.254), reserved, multicast and unspecified ranges.
    return bool(ip.is_global) and not ip.is_multicast


def resolve_host(host: str) -> list[str]:
    """Every address a hostname resolves to. Empty when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return []
    return sorted({info[4][0] for info in infos})


def check_url(url: str, resolver=resolve_host) -> str:
    """Return the URL if it is safe to fetch, else raise :class:`UrlRejected`.

    ``resolver`` is injectable so tests can exercise the DNS-rebinding path
    without touching the network.
    """
    if not url or not isinstance(url, str):
        raise UrlRejected("empty url")

    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlRejected(f"scheme not allowed: {parts.scheme or 'none'!s}")
    if parts.username or parts.password:
        raise UrlRejected("credentials in url")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UrlRejected("no host")
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise UrlRejected(f"internal hostname: {host}")

    # A literal address is checked directly; a name is checked against every
    # address it resolves to, so one private answer is enough to refuse.
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        addresses = resolver(host)
        if not addresses:
            raise HostUnresolved(f"host does not resolve: {host}") from None
    else:
        addresses = [host.strip("[]")]

    for address in addresses:
        if not _is_public(address):
            raise UrlRejected(f"non-public address for {host}: {address}")
    return url


def is_safe_url(url: str, resolver=resolve_host) -> bool:
    """Boolean form of :func:`check_url`."""
    try:
        check_url(url, resolver=resolver)
    except UrlRejected:
        return False
    return True
