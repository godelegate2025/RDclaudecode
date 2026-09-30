"""One post URL in, one report dict out."""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from . import apify, media, metrics, transcribe
from .analysis import analyse
from .media import Frame, MediaError, UrlCheck
from .models import Post
from .platforms import LABELS, detect

log = logging.getLogger("post-audit")

APIFY_HOST = "api.apify.com"


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
