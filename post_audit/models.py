"""The one shape every platform's scraper output is turned into."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Post:
    platform: str
    url: str
    author: str = ""
    author_handle: str = ""
    author_followers: int | None = None
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    posted_at: str = ""              # ISO 8601 as the scraper gave it
    media_type: str = ""             # "video", "image", "carousel" or "text"
    duration_seconds: float | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    video_url: str = ""
    image_urls: list[str] = field(default_factory=list)
    transcript: str = ""
    transcript_source: str = ""      # where the transcript came from, for the report's honesty notes
    top_comments: list[str] = field(default_factory=list)
    sound: str = ""                  # music / audio track name when the platform has one

    def to_dict(self) -> dict:
        data = asdict(self)
        # Signed CDN links expire within hours and mean nothing to a reader.
        data.pop("video_url")
        data.pop("image_urls")
        return data
