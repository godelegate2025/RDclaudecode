"""Fetch one post through Apify and normalise it into a `Post`.

Each network has its own Apify actor with its own input and output shape. The
actor ids can be swapped with environment variables (APIFY_ACTOR_TIKTOK and so
on) when one stops working, which on these networks happens.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import requests

from .models import Post

API = "https://api.apify.com/v2"

DEFAULT_ACTORS = {
    "tiktok": "clockworks~tiktok-scraper",
    "instagram": "apify~instagram-scraper",
    # Facebook's official scrapers take page links: given one post they return
    # its counts and nothing else. Reels and videos go to a scraper built for
    # single video links, which also returns the MP4 and captions.
    "facebook": "apify~facebook-posts-scraper",
    "facebook_video": "apivault_labs~facebook-reels-video-scraper",
    "linkedin": "supreme_coder~linkedin-post",
}


class ScrapeError(RuntimeError):
    """The scraper could not return the post; the message is safe to show."""


def is_facebook_video(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return (parsed.hostname or "").endswith("fb.watch") or path.startswith(("/reel/", "/watch", "/share/r/", "/share/v/")) \
        or "/videos/" in path


def source_for(platform: str, url: str) -> str:
    """Which scraper reads this link: the platform's, or Facebook's video one."""
    return "facebook_video" if platform == "facebook" and is_facebook_video(url) else platform


def actor_for(platform: str) -> str:
    return os.environ.get(f"APIFY_ACTOR_{platform.upper()}", DEFAULT_ACTORS[platform]).replace("/", "~")


def actor_input(platform: str, url: str) -> dict:
    """The smallest input that returns just this one post, with the extras the analysis needs."""
    if platform == "tiktok":
        return {
            "postURLs": [url],
            "resultsPerPage": 1,
            # Stores the mp4 in Apify's key-value store; TikTok's own CDN links
            # refuse downloads that do not carry the scraper's session.
            "shouldDownloadVideos": True,
            "downloadSubtitlesOptions": "DOWNLOAD_SUBTITLES",
            "scrapeRelatedVideos": False,
            "commentsPerPost": 0,
        }
    if platform == "instagram":
        return {"directUrls": [url], "resultsType": "posts", "resultsLimit": 1, "addParentData": False}
    if platform == "facebook":
        return {"startUrls": [{"url": url}], "resultsLimit": 1, "captionText": True}
    if platform == "facebook_video":
        return {
            "workflow": "videoUrls",
            "startUrls": [url],
            "maxResults": 1,
            "outputPreset": "full",
            "downloadMp4": True,
            "includeTranscript": True,
            "enrichDetailPage": True,
            "transcribeWithAsr": False,  # we transcribe on our own server when captions are missing
        }
    if platform == "linkedin":
        return {"urls": [url], "limitPerSource": 1, "deepScrape": True, "numComments": 10, "numLikes": 0}
    raise ValueError(platform)


def fetch_items(platform: str, url: str, token: str, timeout_s: int = 240) -> list[dict]:
    """Run the actor synchronously and return its dataset items."""
    actor = actor_for(platform)
    try:
        response = requests.post(
            f"{API}/acts/{actor}/run-sync-get-dataset-items",
            params={"timeout": timeout_s, "clean": "true"},
            headers={"Authorization": f"Bearer {token}"},
            json=actor_input(platform, url),
            timeout=timeout_s + 30,
        )
    except requests.RequestException as exc:
        raise ScrapeError(f"Could not reach Apify: {exc.__class__.__name__}.") from exc

    if response.status_code in (401, 403):
        raise ScrapeError("Apify rejected the API token. Check APIFY_TOKEN on the server.")
    if response.status_code == 402:
        raise ScrapeError("The Apify account is out of credit for this month.")
    if response.status_code == 404:
        raise ScrapeError(f"The Apify scraper '{actor}' was not found. It may have been renamed.")
    if response.status_code == 408:
        raise ScrapeError("The scraper took too long to fetch this post. Try again in a minute.")
    if response.status_code >= 400:
        raise ScrapeError(f"The scraper failed (HTTP {response.status_code}).")

    try:
        items = response.json()
    except ValueError as exc:
        raise ScrapeError("The scraper returned something that was not JSON.") from exc
    if not isinstance(items, list):
        raise ScrapeError("The scraper returned an unexpected response.")
    return [item for item in items if isinstance(item, dict)]


def _fetch_one(source: str, url: str, token: str, timeout_s: int) -> tuple[Post, dict]:
    items = fetch_items(source, url, token, timeout_s)
    usable = [item for item in items if not _item_error(item)]
    if not usable:
        reason = next((_item_error(item) for item in items if _item_error(item)), "")
        raise ScrapeError(
            "The scraper found no post at that link. Check it is public and not deleted."
            + (f" ({reason})" if reason else "")
        )
    return normalize(source, usable[0], url)


def fetch_post(platform: str, url: str, token: str, timeout_s: int = 240) -> tuple[Post, dict]:
    """Return the normalised post plus extras a platform provides (e.g. subtitle links)."""
    source = source_for(platform, url)
    post, extras = _fetch_one(source, url, token, timeout_s)
    # A Facebook post link can hide a video the posts scraper cannot open; one
    # more cheap call to the video scraper gets the file and captions.
    if source == "facebook" and post.media_type == "video" and not post.video_url:
        try:
            video_post, video_extras = _fetch_one("facebook_video", post.url or url, token, timeout_s)
        except ScrapeError:
            return post, extras
        if video_post.video_url or video_post.caption or video_post.transcript:
            return video_post, video_extras
    return post, extras


def _item_error(item: dict) -> str:
    if item.get("success") is False:
        return str(item.get("videoStatus") or item.get("error") or "the scraper could not read it")[:200]
    error = item.get("error") or item.get("errorDescription") or item.get("errorCode")
    return str(error)[:200] if error else ""


# ---------------------------------------------------------------- normalising

def _get(item: Any, *paths: str) -> Any:
    """First non-empty value among dotted paths — scraper fields drift between versions."""
    for path in paths:
        value = item
        for key in path.split("."):
            if isinstance(value, dict):
                value = value.get(key)
            elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
                value = value[int(key)]
            else:
                value = None
                break
        if value not in (None, "", [], {}):
            return value
    return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _hashtags(caption: str, listed: Any) -> list[str]:
    tags: list[str] = []
    for tag in listed or []:
        name = tag.get("name") if isinstance(tag, dict) else tag
        if isinstance(name, str) and name.strip():
            tags.append(name.strip().lstrip("#"))
    if not tags:
        tags = re.findall(r"#(\w+)", caption or "")
    return list(dict.fromkeys(tags))


def _comment_texts(comments: Any, limit: int = 8) -> list[str]:
    texts = []
    for comment in comments or []:
        text = comment.get("text") if isinstance(comment, dict) else comment
        if isinstance(text, str) and text.strip():
            texts.append(text.strip()[:300])
        if len(texts) >= limit:
            break
    return texts


def normalize(platform: str, item: dict, url: str) -> tuple[Post, dict]:
    return {
        "tiktok": _tiktok,
        "instagram": _instagram,
        "facebook": _facebook,
        "facebook_video": _facebook_video,
        "linkedin": _linkedin,
    }[platform](item, url)


def _tiktok(item: dict, url: str) -> tuple[Post, dict]:
    caption = item.get("text") or ""
    slideshow = bool(item.get("isSlideshow"))
    media_urls = [u for u in item.get("mediaUrls") or [] if isinstance(u, str)]
    post = Post(
        platform="tiktok",
        url=_get(item, "webVideoUrl", "submittedVideoUrl") or url,
        author=_get(item, "authorMeta.nickName", "authorMeta.name") or "",
        author_handle=_get(item, "authorMeta.name") or "",
        author_followers=_int(_get(item, "authorMeta.fans")),
        caption=caption,
        hashtags=_hashtags(caption, item.get("hashtags")),
        posted_at=item.get("createTimeISO") or "",
        media_type="carousel" if slideshow else "video",
        duration_seconds=_float(_get(item, "videoMeta.duration")),
        views=_int(item.get("playCount")),
        likes=_int(item.get("diggCount")),
        comments=_int(item.get("commentCount")),
        shares=_int(item.get("shareCount")),
        saves=_int(item.get("collectCount")),
        video_url="" if slideshow else (media_urls[0] if media_urls else _get(item, "videoMeta.downloadAddr") or ""),
        image_urls=[u for u in item.get("slideshowImageLinks") or [] if isinstance(u, str)][:6]
                   or [u for u in [_get(item, "videoMeta.originalCoverUrl", "videoMeta.coverUrl")] if u],
        top_comments=_comment_texts(item.get("comments")),
        sound=" — ".join(x for x in [_get(item, "musicMeta.musicName"), _get(item, "musicMeta.musicAuthor")] if x),
    )
    return post, {"subtitle_links": _get(item, "videoMeta.subtitleLinks") or []}


def _instagram(item: dict, url: str) -> tuple[Post, dict]:
    caption = item.get("caption") or ""
    kind = (item.get("type") or "").lower()
    children = item.get("childPosts") or []
    images = [c.get("displayUrl") for c in children if isinstance(c, dict) and c.get("displayUrl")]
    images = images or [u for u in item.get("images") or [] if isinstance(u, str)] or \
             [u for u in [item.get("displayUrl")] if u]
    music = item.get("musicInfo") or {}
    post = Post(
        platform="instagram",
        url=item.get("url") or url,
        author=item.get("ownerFullName") or item.get("ownerUsername") or "",
        author_handle=item.get("ownerUsername") or "",
        caption=caption,
        hashtags=_hashtags(caption, item.get("hashtags")),
        posted_at=item.get("timestamp") or "",
        media_type="video" if kind == "video" else "carousel" if kind == "sidecar" else "image",
        duration_seconds=_float(item.get("videoDuration")),
        views=_int(_get(item, "videoPlayCount", "igPlayCount", "videoViewCount")),
        likes=_int(item.get("likesCount")),
        comments=_int(item.get("commentsCount")),
        shares=_int(item.get("reshareCount")),
        video_url=item.get("videoUrl") or "",
        image_urls=images[:6],
        top_comments=_comment_texts(item.get("latestComments")),
        sound=" — ".join(x for x in [music.get("song_name"), music.get("artist_name")] if isinstance(x, str) and x)
              if isinstance(music, dict) else "",
    )
    return post, {}


def _facebook(item: dict, url: str) -> tuple[Post, dict]:
    caption = item.get("text") or ""
    media = [m for m in item.get("media") or [] if isinstance(m, dict)]
    is_video = bool(item.get("isVideo")) or any("video" in str(m.get("__typename", "")).lower() for m in media)
    page = item.get("pageName")
    post = Post(
        platform="facebook",
        url=item.get("url") or url,
        author=_get(item, "user.name") or (page.get("name") if isinstance(page, dict) else page) or "",
        author_handle=page.get("name", "") if isinstance(page, dict) else (page or ""),
        caption=caption,
        hashtags=_hashtags(caption, None),
        posted_at=item.get("time") or "",
        media_type="video" if is_video else ("carousel" if len(media) > 1 else "image" if media else "text"),
        views=_int(_get(item, "viewsCount", "videoPostViewCount")),
        likes=_int(item.get("likes")),
        comments=_int(item.get("comments")),
        shares=_int(item.get("shares")),
        # The posts scraper gives video thumbnails, not the file; frames come
        # from those stills and the transcript Apify extracts.
        video_url=_get(item, "videoUrl", "media.0.videoUrl", "media.0.playable_url") or "",
        image_urls=[m["thumbnail"] for m in media if m.get("thumbnail")][:6],
        transcript=_get(item, "captionText", "videoTranscript", "transcript") or "",
        transcript_source="Facebook captions (via Apify)",
        top_comments=_comment_texts(item.get("topComments")),
    )
    if not post.transcript:
        post.transcript_source = ""
    return post, {}


def _facebook_video(item: dict, url: str) -> tuple[Post, dict]:
    caption = _get(item, "caption", "title") or ""
    transcript = item.get("transcript") or ""
    post = Post(
        platform="facebook",
        url=_get(item, "videoUrl") or url,
        author=item.get("creatorName") or "",
        author_handle=item.get("creatorName") or "",
        author_followers=_int(item.get("creatorFollowers")),
        caption=caption,
        hashtags=_hashtags(caption, item.get("hashtags")),
        posted_at=item.get("publishedAt") or "",
        media_type="video",
        duration_seconds=_float(item.get("durationSeconds")),
        views=_int(item.get("viewCount")),
        likes=_int(item.get("reactionCount")),
        comments=_int(item.get("commentCount")),
        shares=_int(item.get("shareCount")),
        # SD first: plenty for 512px frames, and a fraction of the download.
        video_url=_get(item, "videoMp4SdUrl", "videoMp4Url", "videoMp4HdUrl") or "",
        image_urls=[u for u in [item.get("thumbnailUrl")] if u],
        transcript=transcript,
        transcript_source="Facebook captions (via Apify)" if transcript else "",
    )
    return post, {}


def _linkedin(item: dict, url: str) -> tuple[Post, dict]:
    caption = _get(item, "text", "commentary", "content") or ""
    images = _get(item, "images") or []
    images = [i if isinstance(i, str) else (i.get("url") if isinstance(i, dict) else None) for i in images]
    video = _get(item, "video.url", "video.videoUrl", "videoUrl", "linkedinVideo.videoPlayMetadata.progressiveStreams.0.streamingLocations.0.url") or ""
    post = Post(
        platform="linkedin",
        url=_get(item, "url", "postUrl", "inputUrl") or url,
        author=_get(item, "authorName", "author.name", "author.firstName", "authorFullName") or "",
        author_handle=_get(item, "authorUsername", "author.publicIdentifier", "authorProfileUrl") or "",
        author_followers=_int(_get(item, "authorFollowersCount", "author.followersCount", "author.followers")),
        caption=caption if isinstance(caption, str) else "",
        hashtags=_hashtags(caption if isinstance(caption, str) else "", None),
        posted_at=_get(item, "postedAtISO", "postedAt", "date", "postedDate") or "",
        media_type="video" if video else ("carousel" if len([i for i in images if i]) > 1 else "image" if any(images) else "text"),
        views=_int(_get(item, "numViews", "viewsCount", "impressions")),
        likes=_int(_get(item, "numLikes", "likesCount", "reactionsCount", "totalReactionCount")),
        comments=_int(_get(item, "numComments", "commentsCount")),
        shares=_int(_get(item, "numShares", "sharesCount", "repostsCount")),
        video_url=video if isinstance(video, str) else "",
        image_urls=[i for i in images if isinstance(i, str)][:6],
        top_comments=_comment_texts(_get(item, "comments")),
    )
    return post, {}
