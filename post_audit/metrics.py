"""Engagement arithmetic, done in code so the model never has to guess at maths."""

from __future__ import annotations

from .models import Post


def _rate(part: int | None, whole: int | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 100, 2)


def compute(post: Post, cuts: int | None = None) -> dict:
    counted = [v for v in (post.likes, post.comments, post.shares, post.saves) if v is not None]
    interactions = sum(counted) if counted else None
    metrics = {
        "interactions": interactions,
        # Per 100 views: what share of the people who saw it did something.
        "engagement_rate_by_views": _rate(interactions, post.views),
        "engagement_rate_by_followers": _rate(interactions, post.author_followers),
        "views_per_follower": round(post.views / post.author_followers, 2)
        if post.views and post.author_followers else None,
        "like_rate": _rate(post.likes, post.views),
        "comment_rate": _rate(post.comments, post.views),
        "share_rate": _rate(post.shares, post.views),
        "save_rate": _rate(post.saves, post.views),
        "cuts": cuts,
        "cuts_per_10s": None,
    }
    if cuts is not None and post.duration_seconds:
        window = min(post.duration_seconds, 180)
        metrics["cuts_per_10s"] = round(cuts / window * 10, 1) if window else None
    return metrics
