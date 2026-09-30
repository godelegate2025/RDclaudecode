"""Which network a post URL belongs to, decided before anything is spent on it."""

from __future__ import annotations

from urllib.parse import urlparse

PLATFORMS = ("tiktok", "instagram", "facebook", "linkedin")

LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
}

# Registrable domains only; any subdomain of these is accepted (www., m., vm.).
_DOMAINS = {
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "fb.com": "facebook",
    "linkedin.com": "linkedin",
    "lnkd.in": "linkedin",
}


class UnsupportedURL(ValueError):
    """Not a post on one of the networks this tool reads."""


def detect(url: str) -> tuple[str, str]:
    """Return (platform, cleaned URL), or raise UnsupportedURL with a reason a person can act on."""
    raw = (url or "").strip()
    if not raw:
        raise UnsupportedURL("Paste the link to a post.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise UnsupportedURL("Only web links (https://…) are supported.")
    host = (parsed.hostname or "").lower().rstrip(".")
    for domain, platform in _DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            if not parsed.path.strip("/"):
                raise UnsupportedURL(f"That is the {LABELS[platform]} home page. Paste the link to one post.")
            # Tracking parameters change nothing about which post it is, and
            # some scrapers treat an unfamiliar query string as a different URL.
            # Facebook is the exception: watch/?v= and permalink.php?story_fbid=
            # carry the post id in the query.
            query = parsed.query if platform == "facebook" else ""
            return platform, parsed._replace(scheme="https", query=query, fragment="").geturl()
    raise UnsupportedURL("Paste a TikTok, Instagram, Facebook or LinkedIn post link.")
