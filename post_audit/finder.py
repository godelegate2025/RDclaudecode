"""Find what is performing on a topic or for a profile, across several networks.

One search runs one Apify actor per ticked platform, all at once, and turns
every result into the same row: who posted it, when, what it said, and its
public counts. Engagement is interactions per day since posting, so an old
post with big totals and a new post climbing fast can be compared.

As with the single-post scrapers, each actor id can be swapped with an
environment variable (APIFY_FIND_ACTOR_TIKTOK and so on) when one stops working.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

from . import platforms as audit_platforms
from .apify import ScrapeError, _get, _int, run_actor

PLATFORMS = ("tiktok", "instagram", "facebook", "youtube", "youtube_shorts", "linkedin", "x", "reddit", "meta_ads")

LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "youtube_shorts": "YouTube Shorts",
    "linkedin": "LinkedIn",
    "x": "X",
    "reddit": "Reddit",
    "meta_ads": "Meta Ads",
}

DEFAULT_ACTORS = {
    "tiktok": "clockworks~tiktok-scraper",
    # Hashtag feeds are newest first and Instagram no longer shows top posts,
    # so a topic goes to Instagram's own reel keyword search instead.
    "instagram": "apify~instagram-scraper",
    "instagram_search": "patient_discovery~instagram-search-reels",
    # Facebook's official scraper reads pages; keyword search is a separate actor.
    "facebook": "apify~facebook-posts-scraper",
    "facebook_search": "scraper_one~facebook-posts-search",
    "youtube": "streamers~youtube-scraper",
    "youtube_shorts": "streamers~youtube-scraper",
    # LinkedIn search and a person's own posts are separate actors.
    "linkedin": "harvestapi~linkedin-post-search",
    "linkedin_profile": "harvestapi~linkedin-profile-posts",
    "x": "apidojo~tweet-scraper",
    "reddit": "scrapersdelight~reddit-search-scraper",
    "meta_ads": "apify~facebook-ads-scraper",
}

# Profile links name the network they belong to; registrable domains only.
_DOMAINS = {
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "linkedin.com": "linkedin",
    "x.com": "x",
    "twitter.com": "x",
    "reddit.com": "reddit",
    "facebook.com": "facebook",
    "fb.com": "facebook",
}

# Which network's pasted link each platform can use: Shorts live on a YouTube
# channel, and a page's ads are found from its Facebook link.
_LINK_FROM = {"youtube_shorts": "youtube", "meta_ads": "facebook"}

MAX_ITEMS = int(os.environ.get("FIND_MAX_ITEMS", "50"))
TIMEOUT_S = int(os.environ.get("FIND_TIMEOUT_S", "180"))
X_MIN_LIKES = int(os.environ.get("FIND_X_MIN_LIKES", "50"))
_HANDLE = re.compile(r"^[\w.\-]{1,100}$")


class FindError(ValueError):
    """The search cannot be run as asked; the message is safe to show."""


@dataclass
class Search:
    mode: str                 # "topic" or "profile"
    term: str                 # as typed, for the Search Term column
    handle: str = ""          # profile mode: the account name without the @
    url: str = ""             # profile mode: the profile link, when one was pasted
    url_platform: str = ""    # which network that link belongs to


def parse_search(mode: str, raw: str) -> Search:
    term = " ".join((raw or "").split())
    if not term:
        raise FindError("Type a topic, an @handle or a profile link.")
    if len(term) > 200:
        raise FindError("Keep the search under 200 characters.")
    if mode == "topic":
        return Search("topic", term)
    if mode != "profile":
        raise FindError("Search by topic or by profile.")
    if "/" in term or term.lower().startswith(("http:", "https:", "www.")):
        return _profile_link(term)
    handle = term.lstrip("@")
    if not _HANDLE.match(handle):
        raise FindError("That does not look like a handle. Use @name, or paste the profile link.")
    return Search("profile", term, handle=handle)


def _profile_link(term: str) -> Search:
    raw = term if "://" in term else "https://" + term
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    platform = next((p for d, p in _DOMAINS.items() if host == d or host.endswith("." + d)), "")
    if parsed.scheme not in ("http", "https") or not platform:
        raise FindError("Paste a TikTok, Instagram, YouTube, LinkedIn, X, Reddit or Facebook profile link.")
    parts = [p for p in parsed.path.split("/") if p]
    if parts and parts[0].lower() in ("in", "company", "user", "u", "c", "channel", "school"):
        parts = parts[1:]
    handle = parts[0].lstrip("@") if parts else ""
    if not _HANDLE.match(handle):
        raise FindError("Could not find the account name in that link. Paste the profile page itself.")
    # Facebook profiles without a username are profile.php?id=<number>; the id is the profile.
    query = parsed.query if platform == "facebook" and handle == "profile.php" else ""
    url = parsed._replace(scheme="https", query=query, fragment="").geturl()
    return Search("profile", term, handle=handle, url=url, url_platform=platform)


def clean_platforms(chosen: list[str] | None) -> list[str]:
    picked = [p for p in PLATFORMS if p in set(chosen or [])]
    if not picked:
        raise FindError("Tick at least one platform.")
    return picked


def clean_items(items: Any) -> int:
    count = _int(items) or 10
    return max(1, min(count, MAX_ITEMS))


def actor_for(key: str) -> str:
    return os.environ.get(f"APIFY_FIND_ACTOR_{key.upper()}", DEFAULT_ACTORS[key]).replace("/", "~")


def _profile_url(search: Search, platform: str, default: str) -> str:
    """The pasted link when it is for this network, otherwise one built from the handle."""
    wanted = _LINK_FROM.get(platform, platform)
    return search.url if search.url and search.url_platform == wanted else default


def _phrase(term: str) -> str:
    """Quote a several-word topic: X and Reddit otherwise match posts using any one of the words."""
    return f'"{term}"' if " " in term and '"' not in term else term


def actor_call(platform: str, search: Search, items: int) -> tuple[str, dict]:
    """Which actor to run for this platform, and its input."""
    topic = search.mode == "topic"
    term, handle = search.term, search.handle
    if platform == "tiktok":
        quiet = {"shouldDownloadVideos": False, "shouldDownloadCovers": False,
                 "shouldDownloadSubtitles": False, "shouldDownloadSlideshowImages": False}
        if topic:
            return actor_for("tiktok"), {"searchQueries": [term], "searchSection": "/video", "resultsPerPage": items, **quiet}
        return actor_for("tiktok"), {"profiles": [handle], "profileScrapeSections": ["videos"],
                                     "resultsPerPage": items, **quiet}
    if platform == "instagram":
        if topic:
            # One page of reel search is about a dozen reels.
            return actor_for("instagram_search"), {"query": term, "maxPages": -(-items // 12)}
        start = _profile_url(search, platform, f"https://www.instagram.com/{handle}/")
        return actor_for("instagram"), {"directUrls": [start], "resultsType": "posts",
                                        "resultsLimit": items, "addParentData": False}
    if platform == "facebook":
        if topic:
            return actor_for("facebook_search"), {"query": term, "resultsCount": items, "searchType": "top"}
        page = _profile_url(search, platform, f"https://www.facebook.com/{handle}")
        return actor_for("facebook"), {"startUrls": [{"url": page}], "resultsLimit": items}
    if platform in ("youtube", "youtube_shorts"):
        shorts = platform == "youtube_shorts"
        counts = {"maxResults": 0 if shorts else items, "maxResultsShorts": items if shorts else 0,
                  "maxResultStreams": 0}
        if topic:
            return actor_for(platform), {"searchQueries": [term], **counts}
        channel = _profile_url(search, platform, f"https://www.youtube.com/@{handle}")
        return actor_for(platform), {"startUrls": [{"url": channel}], **counts}
    if platform == "linkedin":
        if topic:
            return actor_for("linkedin"), {"searchQueries": [term], "maxPosts": items, "sortBy": "relevance"}
        profile = _profile_url(search, platform, f"https://www.linkedin.com/in/{handle}/")
        return actor_for("linkedin_profile"), {"targetUrls": [profile], "maxPosts": items}
    if platform == "x":
        if topic:
            # X's "Top" alone still surfaces tweets nobody liked; min_faves keeps it to ones that landed.
            query = f"{_phrase(term)} min_faves:{X_MIN_LIKES}" if X_MIN_LIKES else _phrase(term)
            return actor_for("x"), {"searchTerms": [query], "maxItems": items, "sort": "Top"}
        return actor_for("x"), {"twitterHandles": [handle], "maxItems": items, "sort": "Latest"}
    if platform == "reddit":
        # Reddit search covers both: the phrase ranked by top, or author:<name> for a profile.
        return actor_for("reddit"), {
            "queries": [_phrase(term) if topic else f"author:{handle}"], "sort": "top" if topic else "new", "time": "all",
            "strictSearch": topic, "includeNsfw": False, "includeTopComments": False,
            "maxItems": items, "maxItemsPerQuery": items,
        }
    if platform == "meta_ads":
        if topic:
            # Unordered keywords match any ad using either word; the phrase keeps it on topic.
            start = ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=ALL"
                     f"&q={quote(term)}&search_type=keyword_exact_phrase&media_type=all")
        else:
            start = _profile_url(search, platform, f"https://www.facebook.com/{handle}")
        return actor_for("meta_ads"), {"startUrls": [{"url": start}], "resultsLimit": items}
    raise ValueError(platform)


# ---------------------------------------------------------------- normalising

def _when(value: Any) -> datetime | None:
    """Scrapers give ISO strings, X's 'Wed Oct 10 20:19:24 +0000 2018', or Unix seconds/ms."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return _when(int(text))
    for parse in (lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
                  lambda t: datetime.strptime(t, "%a %b %d %H:%M:%S %z %Y")):
        try:
            moment = parse(text)
        except ValueError:
            continue
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return None


def _text(value: Any, limit: int = 500) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _row(platform: str, *, author: Any, content: Any, url: Any, posted: Any,
         views: Any = None, likes: Any = None, comments: Any = None, shares: Any = None) -> dict:
    return {
        "platform": platform,
        "platform_label": LABELS[platform],
        "author": _text(author, 120).lstrip("@"),
        "content": _text(content),
        "url": url if isinstance(url, str) and url.startswith(("https://", "http://")) else "",
        "posted": _when(posted),
        "views": _int(views),
        "likes": _int(likes),
        "comments": _int(comments),
        "shares": _int(shares),
    }


def _tiktok(item: dict) -> dict:
    return _row("tiktok", author=_get(item, "authorMeta.name", "authorMeta.nickName"), content=item.get("text"),
                url=_get(item, "webVideoUrl", "submittedVideoUrl"), posted=_get(item, "createTimeISO", "createTime"),
                views=item.get("playCount"), likes=item.get("diggCount"),
                comments=item.get("commentCount"), shares=item.get("shareCount"))


def _instagram(item: dict) -> dict:
    # Profiles come from the Apify scraper; topic search returns Instagram's own media shape.
    code = item.get("code")
    kind = "reel" if item.get("product_type") == "clips" else "p"
    return _row("instagram", author=_get(item, "ownerUsername", "user.username"),
                content=_get(item, "caption.text", "caption"),
                url=item.get("url") or (f"https://www.instagram.com/{kind}/{code}/" if code else ""),
                posted=_get(item, "timestamp", "taken_at"),
                views=_get(item, "videoPlayCount", "igPlayCount", "videoViewCount", "play_count", "ig_play_count"),
                likes=_get(item, "likesCount", "like_count"), comments=_get(item, "commentsCount", "comment_count"),
                shares=_get(item, "reshareCount", "share_count"))


def _facebook(item: dict) -> dict:
    # Search returns postText / reactionsCount / a millisecond timestamp; the page scraper text / likes / time.
    page = item.get("pageName")
    return _row("facebook", author=_get(item, "author.name", "user.name") or (page if isinstance(page, str) else ""),
                content=_get(item, "postText", "text"), url=item.get("url"), posted=_get(item, "time", "timestamp"),
                views=_get(item, "viewsCount", "videoPostViewCount"), likes=_get(item, "reactionsCount", "likes"),
                comments=_get(item, "commentsCount", "comments"), shares=_get(item, "sharesCount", "shares"))


def _youtube(platform: str):
    def normalise(item: dict) -> dict:
        title, about = item.get("title") or "", item.get("text") or ""
        return _row(platform, author=_get(item, "channelName", "channelUsername"),
                    content=f"{title} — {about}" if title and about else title or about,
                    url=item.get("url"), posted=_get(item, "date", "uploadDate", "publishedAt"),
                    views=_get(item, "viewCount", "views"), likes=_get(item, "likes", "likeCount"),
                    comments=_get(item, "commentsCount", "commentCount"))
    return normalise


def _linkedin(item: dict) -> dict:
    return _row("linkedin",
                author=_get(item, "author.name", "authorName", "author.publicIdentifier", "authorFullName"),
                content=_get(item, "content", "text", "commentary"),
                url=_get(item, "linkedinUrl", "url", "postUrl"),
                posted=_get(item, "postedAt.date", "postedAt.timestamp", "postedAtISO", "postedAt", "date"),
                views=_get(item, "numViews", "viewsCount", "impressions"),
                likes=_get(item, "engagement.likes", "numLikes", "likesCount", "reactionsCount"),
                comments=_get(item, "engagement.comments", "numComments", "commentsCount"),
                shares=_get(item, "engagement.shares", "numShares", "repostsCount"))


def _x(item: dict) -> dict:
    retweets, quotes = _int(item.get("retweetCount")), _int(item.get("quoteCount"))
    shares = None if retweets is None and quotes is None else (retweets or 0) + (quotes or 0)
    return _row("x", author=_get(item, "author.userName", "author.name"), content=_get(item, "fullText", "text"),
                url=_get(item, "url", "twitterUrl"), posted=item.get("createdAt"), views=item.get("viewCount"),
                likes=item.get("likeCount"), comments=item.get("replyCount"), shares=shares)


def _reddit(item: dict) -> dict:
    title, body = item.get("title") or "", _get(item, "selftext", "body") or ""
    return _row("reddit", author=_get(item, "author", "username"),
                content=f"{title} — {body}" if title and body else title or body,
                # `url` is the linked article on link posts; the permalink is the Reddit thread.
                url=_get(item, "permalink", "url"), posted=_get(item, "created_iso", "created_utc", "createdAt"),
                likes=_get(item, "score", "ups", "upVotes"),
                comments=_get(item, "num_comments", "numberOfComments"))


def _meta_ads(item: dict) -> dict:
    ad_id = _get(item, "adArchiveID", "adArchiveId", "ad_archive_id")
    url = _get(item, "adLibraryURL", "url") or (f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else "")
    return _row("meta_ads", author=_get(item, "pageName", "snapshot.pageName", "page_name"),
                content=_get(item, "snapshot.body.text", "snapshot.cards.0.body", "snapshot.title", "adText"),
                url=url, posted=_get(item, "startDateFormatted", "startDate", "start_date"))


NORMALISERS = {
    "tiktok": _tiktok,
    "instagram": _instagram,
    "facebook": _facebook,
    "youtube": _youtube("youtube"),
    "youtube_shorts": _youtube("youtube_shorts"),
    "linkedin": _linkedin,
    "x": _x,
    "reddit": _reddit,
    "meta_ads": _meta_ads,
}


def _usable(platform: str, item: dict) -> bool:
    if item.get("error") or item.get("errorDescription") or item.get("noResults"):
        return False
    if platform == "reddit" and item.get("dataType") not in (None, "post"):
        return False
    return True


def _expand(raw: list[dict]) -> list[dict]:
    """A Facebook page link comes back as one item per page, with its ads under `results`."""
    out: list[dict] = []
    for item in raw:
        nested = item.get("results")
        if isinstance(nested, list):
            out.extend(r for r in nested if isinstance(r, dict))
        else:
            out.append(item)
    return out


# Meta Ads links are on facebook.com too, but an Ad Library page is not a post.
AUDITABLE = ("tiktok", "instagram", "facebook", "linkedin")


def _auditable(url: str) -> bool:
    """Whether the Post Auditor can open this link."""
    try:
        audit_platforms.detect(url)
    except audit_platforms.UnsupportedURL:
        return False
    return True


def finish(row: dict, term: str, scraped_at: datetime) -> dict:
    """Fill in the columns that depend on when the search ran."""
    posted = row.pop("posted")
    counts = [row[k] for k in ("likes", "comments", "shares") if row[k] is not None]
    days = max(0, (scraped_at - posted).days) if posted else None
    # Interactions per day live; a post from today counts as one day. Small
    # rates keep a decimal so slow posts do not all read as 0.
    per_day = sum(counts) / max(days, 1) if counts and days is not None else None
    row.update(
        search_term=term,
        scraped_at=scraped_at.isoformat(timespec="seconds"),
        posted_at=posted.date().isoformat() if posted else "",
        days_since=days,
        engagement=None if per_day is None else round(per_day) if per_day >= 10 else round(per_day, 1),
        auditable=row["platform"] in AUDITABLE and bool(row["url"]) and _auditable(row["url"]),
    )
    return row


def _one(platform: str, search: Search, items: int, token: str, scraped_at: datetime) -> list[dict]:
    actor, payload = actor_call(platform, search, items)
    raw = run_actor(actor, payload, token, TIMEOUT_S)
    rows = [NORMALISERS[platform](item) for item in _expand(raw) if _usable(platform, item)]
    rows = [finish(row, search.term, scraped_at) for row in rows if row["url"] or row["content"]]
    # Actors often return more than asked; keep the best performers, not the first few.
    return sorted(rows, key=_rank)[:items]


def _rank(row: dict) -> tuple:
    return (row["engagement"] is None, -(row["engagement"] or 0))


def find(search: Search, chosen: list[str], items: int, token: str) -> dict:
    """Run every platform at once; one failing network never hides the others' results."""
    scraped_at = datetime.now(timezone.utc)
    rows: list[dict] = []
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
        futures = {p: pool.submit(_one, p, search, items, token, scraped_at) for p in chosen}
        for platform, future in futures.items():
            try:
                found = future.result()
            except (ScrapeError, FindError) as exc:
                errors.append({"platform": platform, "label": LABELS[platform], "message": str(exc)})
                continue
            except Exception:  # noqa: BLE001 - an actor's odd output must not sink the rest
                errors.append({"platform": platform, "label": LABELS[platform],
                               "message": "The scraper returned something unexpected."})
                continue
            if not found:
                errors.append({"platform": platform, "label": LABELS[platform], "message": "Nothing found."})
            rows.extend(found)
    rows.sort(key=_rank)
    return {
        "mode": search.mode,
        "term": search.term,
        "scraped_at": scraped_at.isoformat(timespec="seconds"),
        "platforms": [{"platform": p, "label": LABELS[p]} for p in chosen],
        "rows": rows,
        "errors": errors,
    }
