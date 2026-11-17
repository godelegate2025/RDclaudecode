"""Whether an uploaded HTML file holds the design, or only a page that loads it.

A page saved from the browser keeps its own markup but not what it pulls in
from elsewhere at run time. The common case is a claude.ai artifact: the saved
file is Claude's viewer (header, Share button) with an empty frame where the
design would load. Scoring that would grade Claude's interface, so the audit
refuses it and says how to get the real file instead.
"""

from __future__ import annotations

import re

CLAUDE_PAGE = (
    "This is the claude.ai page that shows the artifact, saved from the browser, "
    "not the design itself. The design loads into that page from Claude's servers, "
    "so it isn't in the saved file. Upload the design's own HTML file instead."
)

CLAUDE_DESIGN_PAGE = (
    "This is Claude Design's presentation page saved from the browser, not the design "
    "itself. The browser saved the design beside it: open the folder that ends in "
    "\"_files\" next to this file and upload the _bootstrap.html inside it."
)

# The wrapper, not the design: a page Chrome saved from claude.ai itself, or one
# that frames a claudeusercontent.com host. The design's own frame, saved on its
# own, is "saved from" claudeusercontent.com and may load images from there, so
# a bare mention of that host must not count.
SAVED_FROM_CLAUDE = re.compile(r"saved from url=\(\d+\)https?://claude\.ai/", re.IGNORECASE)
FRAMES_CLAUDE = re.compile(r"<iframe\b[^>]*\bsrc=[\"']https?://[^\"']*claudeusercontent\.com", re.IGNORECASE)
DESIGN_BOOTSTRAP = re.compile(r"<iframe\b[^>]*\bsrc=[\"'][^\"']*_files/_bootstrap\.html", re.IGNORECASE)


class NotTheDesign(ValueError):
    """The upload is a shell around the design, not the design."""


def check_markup(html: str) -> str | None:
    """A reason to refuse the file before rendering it, or None."""
    head = html[:2_000_000]
    if SAVED_FROM_CLAUDE.search(head) and DESIGN_BOOTSTRAP.search(head):
        return CLAUDE_DESIGN_PAGE
    if SAVED_FROM_CLAUDE.search(head) or FRAMES_CLAUDE.search(head):
        return CLAUDE_PAGE
    return None


def check_render(data) -> str | None:
    """A reason to refuse the file once rendered, or None.

    A frame pointing at a file that sat beside the upload comes back empty, and
    whatever was meant to show there is missing from every measurement.
    """
    missing = getattr(data, "missing_frames", None) or []
    if not missing:
        return None
    shown = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
    return (
        f"Part of this page loads from other files that weren't uploaded with it ({shown}), "
        "so they'd show up empty and the report would measure the wrong thing. "
        "Upload one standalone HTML file that contains the whole design."
    )
