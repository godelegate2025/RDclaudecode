"""Recognise pages that are a bot wall rather than the site being audited.

A WAF challenge renders as a real page: it has fonts, colours and contrast, and
the audit will happily score it. The resulting report looks authoritative and
describes nothing the site owner can act on, so a blocked fetch has to be
reported as a blocked fetch.
"""

from __future__ import annotations

from dataclasses import dataclass

# Phrases that only appear on interstitials, not on real pages.
BLOCK_PHRASES = [
    "sorry, you have been blocked",
    "you are unable to access",
    "attention required!",
    "checking your browser before accessing",
    "just a moment...",
    "enable javascript and cookies to continue",
    "access denied",
    "request blocked",
    "verify you are human",
    "please verify you are a human",
    "unusual traffic from your computer",
    "this website is using a security service to protect itself",
    "ray id:",
    "error 1020",
    "pardon our interruption",
    "are you a robot",
]

# Vendor markers that appear in the markup of challenge pages.
VENDOR_MARKERS = {
    "cloudflare": "Cloudflare",
    "perimeterx": "PerimeterX",
    "incapsula": "Imperva Incapsula",
    "imperva": "Imperva",
    "akamai": "Akamai",
    "datadome": "DataDome",
    "distil": "Distil Networks",
    "recaptcha": "reCAPTCHA",
    "hcaptcha": "hCaptcha",
}


@dataclass
class BlockVerdict:
    blocked: bool
    reason: str = ""
    vendor: str | None = None
    status: int | None = None

    def __bool__(self) -> bool:
        return self.blocked


def detect(data) -> BlockVerdict:
    """Decide whether the page we rendered is the site, or a wall in front of it."""
    probe = data.probe
    status = data.status
    haystack = " ".join(
        part.lower()
        for part in (probe.get("title") or "", probe.get("body_text_sample") or "")
        if part
    )

    matched = next((p for p in BLOCK_PHRASES if p in haystack), None)
    vendor = next((name for key, name in VENDOR_MARKERS.items() if key in haystack), None)

    # An explicit refusal status is the strongest signal.
    if status in (401, 403, 406, 429) or (status is not None and status >= 500):
        detail = f"the site returned HTTP {status}"
        if matched:
            detail += " and served a bot-protection page"
        return BlockVerdict(True, detail, vendor, status)

    # A 200 that is nonetheless a challenge page: short, and saying so.
    if matched:
        thin = len(probe.get("text_runs", [])) < 40
        if thin or vendor:
            return BlockVerdict(
                True, f"the page served a bot-protection interstitial (“{matched}”)", vendor, status
            )

    return BlockVerdict(False)


def explain(verdict: BlockVerdict, host: str) -> str:
    """A message for someone who just wanted their site audited."""
    vendor = f" ({verdict.vendor})" if verdict.vendor else ""
    return (
        f"{host} blocked the audit{vendor}: {verdict.reason}. "
        "What rendered was the protection page, not the site, so any report would "
        "describe the wall rather than your design. "
        "If you control the site, allowlist the auditor's IP or run the audit from a "
        "network the protection trusts; otherwise audit a page that is not behind it."
    )
