"""Chromium lifecycle. One launch serves both the audit and the PDF print."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

# A pre-installed Chromium is preferred where one exists (sandboxes, ARM hosts
# where Playwright ships no build of its own).
CHROMIUM_PATHS = [
    os.environ.get("AUDIT_CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium",
]

# Encrypted Client Hello hides the SNI, which TLS-inspecting corporate/CI proxies
# cannot parse — they drop the connection. Turning ECH off restores the handshake;
# certificate verification is untouched.
DEFAULT_ARGS = [
    "--disable-features=EncryptedClientHello",
    # Containers give /dev/shm 64MB; without this Chromium dies on heavy pages.
    "--disable-dev-shm-usage",
]

# AUDIT_CHROMIUM_ARGS replaces the defaults outright; AUDIT_CHROMIUM_EXTRA_ARGS
# adds to them, which is what a container needs (it appends --no-sandbox without
# silently dropping the /dev/shm and ECH flags).
LAUNCH_ARGS = (
    os.environ["AUDIT_CHROMIUM_ARGS"].split()
    if os.environ.get("AUDIT_CHROMIUM_ARGS")
    else DEFAULT_ARGS
) + os.environ.get("AUDIT_CHROMIUM_EXTRA_ARGS", "").split()


def launch(pw) -> Browser:
    last_error = None
    for path in CHROMIUM_PATHS:
        try:
            if path and Path(path).exists():
                return pw.chromium.launch(executable_path=path, args=LAUNCH_ARGS)
        except PlaywrightError as exc:  # pragma: no cover - environment dependent
            last_error = exc
    try:
        return pw.chromium.launch(args=LAUNCH_ARGS)
    except PlaywrightError as exc:
        raise RuntimeError(
            f"Could not launch Chromium ({exc}). Set AUDIT_CHROMIUM_PATH to a Chromium binary."
        ) from last_error or exc


@contextmanager
def browser_session():
    """Yield one browser for a whole audit — collection and PDF print alike.

    Launching Chromium costs about a second and a few hundred MB; doing it twice
    per audit doubles that for no benefit. Long-running services should hold one
    session open and pass the browser in.
    """
    with sync_playwright() as pw:
        browser = launch(pw)
        try:
            yield browser
        finally:
            browser.close()
