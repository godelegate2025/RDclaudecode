"""URL validation for a public audit endpoint.

A hosted browser that fetches whatever a stranger types is a server-side request
forgery engine: it will happily render a cloud metadata endpoint, an internal
admin panel, or a service on localhost and hand back a screenshot of it. Every
URL is resolved and checked against the blocked ranges before Chromium sees it,
and again on each redirect hop.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}
HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
MAX_URL_LENGTH = 2048

# Hosts that resolve inside the infrastructure rather than out on the internet.
BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",         # "this network"
        "10.0.0.0/8",        # RFC1918 private
        "100.64.0.0/10",     # carrier-grade NAT
        "127.0.0.0/8",       # loopback
        "169.254.0.0/16",    # link-local — cloud metadata lives at 169.254.169.254
        "172.16.0.0/12",     # RFC1918 private
        "192.0.0.0/24",      # IETF protocol assignments
        "192.168.0.0/16",    # RFC1918 private
        "198.18.0.0/15",     # benchmarking
        "224.0.0.0/4",       # multicast
        "240.0.0.0/4",       # reserved
        "::1/128",           # IPv6 loopback
        "fc00::/7",          # IPv6 unique-local
        "fe80::/10",         # IPv6 link-local
        "ff00::/8",          # IPv6 multicast
    )
]

# Names that resolve to infrastructure on some clouds regardless of IP.
BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}


class UnsafeURL(ValueError):
    """The URL points somewhere a public service must not fetch."""


@dataclass
class SafeURL:
    url: str
    hostname: str
    addresses: list[str]


def _check_address(raw: str) -> None:
    address = ipaddress.ip_address(raw)
    for network in BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            raise UnsafeURL(
                f"{raw} is in a reserved or private range ({network}) and cannot be audited."
            )


def validate(url: str) -> SafeURL:
    """Return the URL with its resolved addresses, or raise UnsafeURL."""
    if not url or len(url) > MAX_URL_LENGTH:
        raise UnsafeURL("Provide a URL shorter than 2048 characters.")

    candidate = url.strip()
    if not HAS_SCHEME.match(candidate):
        candidate = "https://" + candidate

    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURL("That URL is malformed.") from exc

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURL(f"Only http and https are supported (got {parsed.scheme or 'none'}).")

    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise UnsafeURL("That URL has no hostname.")
    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(".internal") or hostname.endswith(".local"):
        raise UnsafeURL(f"{hostname} is an internal name and cannot be audited.")

    # A bare IP literal is checked directly; a name is checked on every address
    # it resolves to, so a domain pointing at 127.0.0.1 is caught too.
    try:
        _check_address(hostname)
        addresses = [hostname]
    except ValueError as exc:
        if isinstance(exc, UnsafeURL):
            raise
        try:
            infos = socket.getaddrinfo(hostname, port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as err:
            raise UnsafeURL(f"Could not resolve {hostname}.") from err
        addresses = sorted({info[4][0] for info in infos})
        if not addresses:
            raise UnsafeURL(f"Could not resolve {hostname}.") from None
        for address in addresses:
            _check_address(address)

    return SafeURL(url=candidate, hostname=hostname, addresses=addresses)


def guard_route(route, request) -> None:
    """Playwright route handler: re-check every navigation, including redirects.

    DNS can resolve differently between the check and the fetch, and a redirect
    can point anywhere, so the document requests are validated again here.
    """
    if request.is_navigation_request():
        try:
            validate(request.url)
        except UnsafeURL:
            route.abort("blockedbyclient")
            return
    route.continue_()
