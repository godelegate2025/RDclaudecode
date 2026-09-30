"""Ask Claude why the post works, and get the answer back as a fixed structure.

The report page renders these fields directly, so the response is constrained
to a JSON schema rather than parsed out of prose.
"""

from __future__ import annotations

import base64
import json
import os

import anthropic

from .media import Frame
from .models import Post
from .platforms import LABELS

MODEL = os.environ.get("POST_AUDIT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("POST_AUDIT_EFFORT", "medium")

HOOK_TYPES = [
    "question", "bold_claim", "curiosity_gap", "pattern_interrupt", "relatable_pain",
    "story_open", "how_to", "list", "contrarian", "visual_surprise", "social_proof", "other",
]

_str = {"type": "string"}
_str_list = {"type": "array", "items": _str}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "hook", "structure", "pacing", "caption", "performance",
                 "why_it_works", "takeaways", "avoid", "limits"],
    "properties": {
        "verdict": {**_str, "description": "One or two sentences: why this post performed."},
        "hook": {
            "type": "object",
            "additionalProperties": False,
            "required": ["opening", "type", "why_it_grabs", "score", "score_reason"],
            "properties": {
                "opening": {**_str, "description": "What the viewer sees and hears in the first 3 seconds, quoting any words."},
                "type": {"type": "string", "enum": HOOK_TYPES},
                "why_it_grabs": _str,
                "score": {"type": "integer", "description": "1-10"},
                "score_reason": _str,
            },
        },
        "structure": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["beat", "timing", "what_happens"],
                "properties": {
                    "beat": {**_str, "description": "e.g. Hook, Setup, Payoff, CTA"},
                    "timing": {**_str, "description": "e.g. 0-3s, or 'slide 2' for carousels; empty if unknown"},
                    "what_happens": _str,
                },
            },
        },
        "pacing": {
            "type": "object",
            "additionalProperties": False,
            "required": ["editing", "on_screen_text", "audio"],
            "properties": {"editing": _str, "on_screen_text": _str, "audio": _str},
        },
        "caption": {
            "type": "object",
            "additionalProperties": False,
            "required": ["assessment", "cta", "cta_strength"],
            "properties": {
                "assessment": _str,
                "cta": {**_str, "description": "The call to action, quoted; empty if none."},
                "cta_strength": {"type": "string", "enum": ["none", "weak", "clear", "strong"]},
            },
        },
        "performance": {
            "type": "object",
            "additionalProperties": False,
            "required": ["read", "signals"],
            "properties": {
                "read": {**_str, "description": "What the numbers say about how people responded."},
                "signals": _str_list,
            },
        },
        "why_it_works": _str_list,
        "takeaways": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "how_to_apply"],
                "properties": {"title": _str, "how_to_apply": _str},
            },
        },
        "avoid": _str_list,
        "limits": {**_str, "description": "What was missing from the evidence and how it limits the read; empty if nothing."},
    },
}

SYSTEM = """You audit social media posts for Redefine, a creative agency. The team pastes in a post that is \
performing well and wants to know exactly why, so they can reuse what works in their own and their \
clients' content.

You receive the post's caption, metrics computed from the platform's public numbers, a transcript when \
one exists, and still frames: several from the first three seconds (the hook window) and more spread \
across the rest of the video. For photo and carousel posts you receive the images instead.

How to work:
- Ground every claim in the evidence you were given: quote the words, describe the frame, cite the number.
- The frames are samples, not the full video. Infer motion and editing only as far as consecutive frames \
and the cut count support, and say so when you are inferring.
- Engagement rates vary a lot by platform, niche and account size. Describe what the numbers show \
(for example, a save rate that is high relative to likes suggests reference value) without quoting \
industry benchmarks as fact.
- Takeaways must be specific enough to brief an editor or copywriter tomorrow: name the technique and \
how to apply it. Give 3 to 5. Avoid generic advice like "be authentic" or "post consistently".
- Use plain, direct English. Keep each field short; the report is read on a screen.
- Put anything you could not assess (no video, no transcript, missing metrics) in `limits`."""


class AnalysisError(RuntimeError):
    """Claude could not produce an analysis; the message is safe to show."""


def _image_block(frame: Frame) -> dict:
    data = base64.standard_b64encode(frame.path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _frame_label(frame: Frame) -> str:
    if frame.seconds is None:
        return f"{frame.label.capitalize()}:"
    window = "hook window" if frame.label == "hook" else "body"
    return f"Frame at {frame.seconds:.1f}s ({window}):"


def build_content(post: Post, metrics: dict, frames: list[Frame]) -> list[dict]:
    facts = {
        "platform": LABELS.get(post.platform, post.platform),
        **{k: v for k, v in post.to_dict().items() if k not in ("platform", "transcript", "top_comments")},
        "metrics": metrics,
    }
    content: list[dict] = [{"type": "text", "text": "Post data:\n" + json.dumps(facts, indent=1, ensure_ascii=False)}]
    if post.transcript:
        content.append({"type": "text", "text": f"Transcript ({post.transcript_source or 'speech to text'}):\n{post.transcript[:6000]}"})
    else:
        content.append({"type": "text", "text": "Transcript: none available."})
    if post.top_comments:
        content.append({"type": "text", "text": "Sample of top comments:\n" + "\n".join(f"- {c}" for c in post.top_comments)})
    for frame in frames:
        content.append({"type": "text", "text": _frame_label(frame)})
        content.append(_image_block(frame))
    if not frames:
        content.append({"type": "text", "text": "No images or video frames could be retrieved for this post."})
    content.append({"type": "text", "text": "Audit this post."})
    return content


def analyse(post: Post, metrics: dict, frames: list[Frame], client: anthropic.Anthropic | None = None) -> tuple[dict, dict]:
    """Return (analysis, usage) — usage feeds the log line, so cost is visible per audit."""
    client = client or anthropic.Anthropic()
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": build_content(post, metrics, frames)}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
            # If a safety classifier declines, the API retries on the model
            # Anthropic recommends for that case instead of failing the audit.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise AnalysisError("The Claude API key was rejected. Check ANTHROPIC_API_KEY on the server.") from exc
    except anthropic.RateLimitError as exc:
        raise AnalysisError("The Claude API is rate limiting this account. Try again in a minute.") from exc
    except anthropic.BadRequestError as exc:
        raise AnalysisError(f"The Claude API refused the request: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AnalysisError(f"The Claude API failed (HTTP {exc.status_code}). Try again shortly.") from exc
    except anthropic.APIConnectionError as exc:
        raise AnalysisError("Could not reach the Claude API.") from exc

    if response.stop_reason == "refusal":
        raise AnalysisError("Claude declined to analyse this post.")
    if response.stop_reason == "max_tokens":
        raise AnalysisError("The analysis ran out of room. Try again.")
    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        analysis = json.loads(text)
    except ValueError as exc:
        raise AnalysisError("Claude returned an analysis that could not be read.") from exc

    usage = {
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return analysis, usage
