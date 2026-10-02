"""One post URL in, one report dict out."""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from . import apify, media, metrics, transcribe
from .analysis import analyse
from .media import Frame, MediaError, UrlCheck
from .models import Post
from .platforms import LABELS, detect

log = logging.getLogger("post-audit")

APIFY_HOST = "api.apify.com"


class NotEnoughToAudit(RuntimeError):
    """The scraper returned too little to analyse; checked before any Claude spend."""


def resolve_share_link(platform: str, url: str, check: UrlCheck) -> str:
    """Follow a Facebook share link (facebook.com/share/…, fb.watch) to the post it points at.

    Scrapers treat the share link as an unknown page and come back with the
    counts but no caption or media. The redirect is public, so follow it — but
    only while it stays on Facebook, and never into the login wall.
    """
    parsed = urlparse(url)
    if platform != "facebook" or not (parsed.path.startswith("/share/") or parsed.hostname == "fb.watch"):
        return url
    current = url
    for _ in range(5):
        try:
            check(current)
            response = requests.get(current, allow_redirects=False, timeout=15,
                                    headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
        except Exception:  # noqa: BLE001 - resolving is best effort; the scraper may still cope
            return url
        response.close()
        location = response.headers.get("location")
        if not response.is_redirect or not location:
            break
        target = urljoin(current, location)
        host = (urlparse(target).hostname or "").lower()
        if not (host == "facebook.com" or host.endswith(".facebook.com")) or "/login" in urlparse(target).path:
            break
        current = target
        if not urlparse(current).path.startswith("/share/"):
            # Drop the tracking query the redirect adds, except where the id lives in it.
            path = urlparse(current).path
            return current if path.startswith(("/watch", "/permalink.php", "/story.php")) \
                else urlparse(current)._replace(query="", fragment="").geturl()
    return url


def _media_headers(url: str, apify_token: str) -> dict:
    # TikTok videos are stored in Apify's key-value store, which needs the
    # token; nothing else gets it.
    return {"Authorization": f"Bearer {apify_token}"} if urlparse(url).hostname == APIFY_HOST else {}


def _subtitles(links: list, work_dir: Path, check: UrlCheck) -> str:
    """Text of the best subtitle track TikTok offers, English first."""
    tracks = [link for link in links if isinstance(link, dict) and link.get("downloadLink")]
    tracks.sort(key=lambda link: 0 if str(link.get("language", "")).lower().startswith("en") else 1)
    for track in tracks[:2]:
        path = work_dir / "subtitles.vtt"
        try:
            media.download(track["downloadLink"], path, check, 2 * 1024 * 1024, timeout=20)
        except Exception:  # noqa: BLE001 - subtitles are optional
            continue
        text = media.vtt_to_text(path.read_text(encoding="utf-8", errors="replace"))
        if text:
            return text
    return ""


def _video_evidence(post: Post, work_dir: Path, check: UrlCheck, apify_token: str) -> tuple[list[Frame], int | None, list[str]]:
    """Frames, cut count, and notes on anything that could not be done."""
    notes: list[str] = []
    video = work_dir / "video.mp4"
    try:
        media.download(post.video_url, video, check, media.MAX_VIDEO_BYTES,
                       headers=_media_headers(post.video_url, apify_token), timeout=120)
    except MediaError as exc:
        notes.append(f"Video could not be downloaded: {exc}")
        return [], None, notes

    duration = media.probe_duration(video) or post.duration_seconds
    if duration and not post.duration_seconds:
        post.duration_seconds = round(duration, 1)
    try:
        frames = media.extract_frames(video, work_dir, duration)
    except MediaError as exc:
        notes.append(f"The video could not be read: {exc}")
        return [], None, notes
    cuts = media.count_cuts(video)

    if not post.transcript:
        audio = media.extract_audio(video, work_dir / "audio.wav")
        if audio and transcribe.available():
            post.transcript = transcribe.transcribe(audio)
            post.transcript_source = "speech to text" if post.transcript else ""
        elif not audio:
            notes.append("The video has no audio track.")
    return frames, cuts, notes


def frame_previews(frames: list[Frame]) -> list[dict]:
    return [
        {
            "seconds": frame.seconds,
            "label": frame.label,
            "src": "data:image/jpeg;base64," + base64.b64encode(frame.path.read_bytes()).decode(),
        }
        for frame in frames
    ]


def audit_post(url: str, work_dir: Path, *, apify_token: str, check: UrlCheck, client=None) -> dict:
    started = time.time()
    platform, clean_url = detect(url)
    clean_url = resolve_share_link(platform, clean_url, check)
    post, extras = apify.fetch_post(platform, clean_url, apify_token)
    log.info("fetched %s post by %s in %.1fs", platform, post.author_handle or post.author or "?", time.time() - started)

    notes: list[str] = []
    frames: list[Frame] = []
    cuts = None

    if not post.transcript and extras.get("subtitle_links"):
        post.transcript = _subtitles(extras["subtitle_links"], work_dir, check)
        post.transcript_source = "TikTok subtitles" if post.transcript else ""

    if post.video_url:
        frames, cuts, notes = _video_evidence(post, work_dir, check, apify_token)
    if not frames and post.image_urls:
        frames = media.fetch_images(post.image_urls, work_dir, check)
        if post.media_type == "video":
            notes.append("Only the video's thumbnail could be analysed, not the video itself.")
    if not frames and post.media_type != "text":
        notes.append("No images or frames could be retrieved.")

    if not (post.caption.strip() or post.transcript or frames):
        raise NotEnoughToAudit(
            f"{LABELS[platform]} only gave the scraper this post's counts: no caption, video or images, "
            "so there is nothing to analyse. Open the post itself and copy its link from the address bar "
            "(not the Share button), or try another post. The Claude analysis was skipped, so this cost only the scrape."
        )

    numbers = metrics.compute(post, cuts)
    analysis, usage = analyse(post, numbers, frames, client=client)
    log.info("analysed %s post in %.1fs total — %s in / %s out tokens on %s",
             platform, time.time() - started, usage["input_tokens"], usage["output_tokens"], usage["model"])

    return {
        "platform": platform,
        "platform_label": LABELS[platform],
        "post": post.to_dict(),
        "metrics": numbers,
        "analysis": analysis,
        "frames": frame_previews(frames),
        "notes": notes,
    }
