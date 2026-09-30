"""Audit a live social media post: why it performed, and what to reuse.

A post URL goes to an Apify scraper for the caption, metrics and media; the
video is cut into frames (dense over the first three seconds, where the hook
lives) and transcribed; Claude reads all of it and returns a structured
breakdown. Nothing here is shared with `website_audit`, so this package can
move to its own service without untangling anything.
"""
