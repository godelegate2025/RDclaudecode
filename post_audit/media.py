"""Download a post's media and turn it into what Claude can read: stills and audio.

Claude cannot watch a video, so the video becomes frames — dense over the
first three seconds, where the hook is won or lost, then spread across the
rest — plus a count of hard cuts as a pacing signal.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
HOOK_TIMES = (0.0, 0.6, 1.2, 1.8, 2.4, 3.0)
BODY_FRAMES = 6
FRAME_WIDTH = 512
# Pacing and transcription only look at this much of a long video; the hook and
# the structure of a short-form post are well inside it.
ANALYSE_SECONDS = 180

UrlCheck = Callable[[str], object]


class MediaError(RuntimeError):
    """Media could not be fetched or processed; the message is safe to show."""


@dataclass
class Frame:
    path: Path
    seconds: float | None     # None for a still image (photo post, carousel slide)
    label: str


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # a pip-installed static build, handy for local development
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise MediaError("ffmpeg is not installed on the server.") from exc


def download(url: str, dest: Path, check: UrlCheck, max_bytes: int,
             headers: dict | None = None, timeout: int = 60) -> Path:
    """Stream `url` to `dest`, re-checking every redirect hop against `check`.

    The URL comes from scraper output, not from us, so each hop must pass the
    same public-address guard the website auditor uses before we connect.
    """
    current = url
    for _ in range(6):
        if urlparse(current).scheme != "https":
            raise MediaError("Refused a media link that is not https.")
        try:
            check(current)
        except ValueError as exc:  # the guard's UnsafeURL, or a host that will not resolve
            raise MediaError(f"Refused a media link: {exc}") from exc
        try:
            response = requests.get(current, stream=True, timeout=timeout, allow_redirects=False,
                                    headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
        except requests.RequestException as exc:
            raise MediaError(f"Could not download the media ({exc.__class__.__name__}).") from exc
        if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
            current = urljoin(current, response.headers.get("location", ""))
            response.close()
            headers = None  # never forward credentials to wherever a redirect points
            continue
        if response.status_code >= 400:
            response.close()
            raise MediaError(f"The media link answered HTTP {response.status_code}; it may have expired.")
        size = 0
        with response, dest.open("wb") as fh:
            for chunk in response.iter_content(1024 * 256):
                size += len(chunk)
                if size > max_bytes:
                    raise MediaError("The media file is too large to analyse.")
                fh.write(chunk)
        if size == 0:
            raise MediaError("The media download was empty.")
        return dest
    raise MediaError("The media link redirected too many times.")


def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Processing the video took too long.") from exc


def probe_duration(video: Path) -> float | None:
    """Seconds, parsed from ffmpeg's banner; ffprobe is not in every static build."""
    result = _run([ffmpeg_bin(), "-hide_banner", "-i", str(video)], timeout=30)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return None
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def has_audio(video: Path) -> bool:
    result = _run([ffmpeg_bin(), "-hide_banner", "-i", str(video)], timeout=30)
    return bool(re.search(r"Stream #\S+.*Audio:", result.stderr))


def frame_times(duration: float | None) -> list[tuple[float, str]]:
    """(seconds, label) pairs: the hook window, then the body evenly spaced."""
    if not duration or duration <= 0:
        return [(t, "hook") for t in HOOK_TIMES]
    end = max(0.0, duration - 0.2)
    times = [(min(t, end), "hook") for t in HOOK_TIMES if t <= duration]
    if duration > 4:
        span = end - 3.5
        for i in range(BODY_FRAMES):
            times.append((round(3.5 + span * (i + 1) / BODY_FRAMES, 2), "body"))
    seen, unique = set(), []
    for t, label in times:
        key = round(t, 1)
        if key not in seen:
            seen.add(key)
            unique.append((t, label))
    return unique


def extract_frames(video: Path, out_dir: Path, duration: float | None) -> list[Frame]:
    frames = []
    for i, (t, label) in enumerate(frame_times(duration)):
        out = out_dir / f"frame-{i:02d}.jpg"
        out.unlink(missing_ok=True)  # a failed extraction must not pick up an older file
        _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
              "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", "-y", str(out)], timeout=60)
        if out.exists() and out.stat().st_size > 0:
            frames.append(Frame(out, t, label))
    if not frames:
        raise MediaError("Could not read any frames from the video.")
    return frames


def count_cuts(video: Path, threshold: float = 0.3) -> int | None:
    """Hard cuts in the analysed window — a rough, honest pacing signal."""
    result = _run([ffmpeg_bin(), "-hide_banner", "-t", str(ANALYSE_SECONDS), "-i", str(video), "-an",
                   "-vf", f"scale=160:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                  timeout=180)
    if result.returncode != 0:
        return None
    return len(re.findall(r"pts_time:", result.stderr))


def extract_audio(video: Path, dest: Path) -> Path | None:
    if not has_audio(video):
        return None
    _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-t", str(ANALYSE_SECONDS), "-i", str(video),
          "-vn", "-ac", "1", "-ar", "16000", "-y", str(dest)], timeout=120)
    return dest if dest.exists() and dest.stat().st_size > 1000 else None


def normalise_image(source: Path, dest: Path) -> Path | None:
    """Re-encode any still (webp, heic, huge jpeg) to a modest jpeg Claude accepts."""
    _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(source),
          "-vf", "scale='min(768,iw)':-2", "-frames:v", "1", "-q:v", "4", "-y", str(dest)], timeout=60)
    return dest if dest.exists() and dest.stat().st_size > 0 else None


def fetch_images(urls: list[str], work_dir: Path, check: UrlCheck, limit: int = 6) -> list[Frame]:
    frames = []
    for i, url in enumerate(urls[:limit]):
        raw = work_dir / f"image-{i:02d}.bin"
        try:
            download(url, raw, check, MAX_IMAGE_BYTES, timeout=30)
            image = normalise_image(raw, work_dir / f"image-{i:02d}.jpg")
        except Exception:  # noqa: BLE001 - an unsafe or dead link just drops that image
            continue
        if image:
            frames.append(Frame(image, None, f"image {i + 1}"))
    return frames


def vtt_to_text(vtt: str) -> str:
    """Plain spoken text from a WebVTT subtitle file, repeats removed."""
    lines = []
    for line in vtt.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")) or "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines)
