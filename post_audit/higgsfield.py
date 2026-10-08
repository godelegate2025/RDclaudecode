"""Turn a finished audit into a Higgsfield brief for the team's own post.

The person fills in what their post is about; Claude rewrites the audit's
verdict and takeaways into a shot list where every shot is a prompt that can
be pasted into Higgsfield on its own: a start-frame image prompt, then an
image-to-video prompt with one camera move. AI video renders words badly, so
on-screen text and voiceover are kept out of the prompts and handed to the
editor instead.
"""

from __future__ import annotations

import json
import os

import anthropic

from .analysis import AnalysisError, call_claude

EFFORT = os.environ.get("PROMPT_EFFORT", "medium")
FORMATS = ("video", "carousel", "image")
MAX_FIELD = 600

_str = {"type": "string"}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["concept", "why_it_will_work", "aspect_ratio", "style_anchor", "shots", "caption",
                 "hashtags", "production_notes"],
    "properties": {
        "concept": {**_str, "description": "The idea for the new post in one or two sentences."},
        "why_it_will_work": {
            "type": "array", "items": _str,
            "description": "2-4 bullets tying the concept to the audit: which takeaway each part applies.",
        },
        "aspect_ratio": {**_str, "description": "e.g. 9:16"},
        "style_anchor": {
            **_str,
            "description": "One paragraph describing the look shared by every shot (subject, wardrobe, setting, "
                           "lighting, colour grade, lens feel), repeated so separate generations match.",
        },
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "timing", "purpose", "start_frame_prompt", "video_prompt", "camera_motion",
                             "on_screen_text", "voiceover", "sound"],
                "properties": {
                    "label": {**_str, "description": "e.g. Hook, Problem, Payoff, CTA, or Slide 1"},
                    "timing": {**_str, "description": "e.g. 0-3s; empty for carousel slides and single images"},
                    "purpose": {**_str, "description": "What this shot does for the viewer, and the takeaway it applies."},
                    "start_frame_prompt": {
                        **_str,
                        "description": "A complete, self-contained image prompt for the first frame (or the slide). "
                                       "Include the style anchor's details. No words, logos or captions in the image.",
                    },
                    "video_prompt": {
                        **_str,
                        "description": "A complete, self-contained image-to-video prompt: the action, what moves, "
                                       "pace and mood. Empty for carousel slides and single images.",
                    },
                    "camera_motion": {**_str, "description": "One camera move, e.g. slow dolly in, crash zoom, handheld follow, static."},
                    "on_screen_text": {**_str, "description": "Overlay text added in editing, quoted; empty if none."},
                    "voiceover": {**_str, "description": "Spoken line, quoted; empty if none."},
                    "sound": {**_str, "description": "Music or sound design cue; empty if none."},
                },
            },
        },
        "caption": {**_str, "description": "The post caption, ending with the call to action."},
        "hashtags": {"type": "array", "items": _str},
        "production_notes": {
            "type": "array", "items": _str,
            "description": "2-4 practical tips for generating and editing this in Higgsfield.",
        },
    },
}

SYSTEM = """You are a creative director at Redefine, a creative agency. The team has just audited a social \
post that performed well. Now they want to make their own post that uses the same winning moves, and \
generate it with Higgsfield, an AI image and video generator.

You receive the audit (verdict, hook, structure, pacing, caption, why it works, takeaways, what to avoid) \
and the team's brief for their own post. Write a new post, not a copy: apply the audit's techniques to the \
brief's topic, brand and audience. Keep what to avoid out of it.

How Higgsfield is used:
- Each shot is generated separately, so every prompt must stand on its own. Repeat the key look details \
from the style anchor inside each start_frame_prompt so the shots match.
- A shot is usually made in two steps: an image for the first frame, then image-to-video with one camera \
move. Keep each video shot to roughly 3-8 seconds of action, and give it one clear camera move.
- AI video renders text and logos badly. Never ask for words, captions, logos or UI inside a prompt; put \
them in on_screen_text for the editor instead.
- Write prompts as concrete visual description: subject, action, setting, lighting, lens and framing, \
mood. No vague words like "engaging" or "viral".

Rules:
- The hook shot comes first and must do in the first 3 seconds what the audit says made the original's \
hook work.
- Fit the shot count and timings to the requested length. For a carousel, one shot per slide; for a \
single image, one shot. Leave video_prompt and timing empty for slides and single images.
- If the brief leaves something out, choose something sensible for the brand and audience rather than \
asking.
- Do not invent facts about the brand (prices, results, awards) that the brief does not give.
- Plain, direct English. Keep fields tight; this is pasted straight into a tool."""


def _clip(value, limit: int = MAX_FIELD) -> str:
    return str(value or "").strip()[:limit]


def clean_brief(brief: dict) -> dict:
    """The brief as the person typed it, trimmed; raises AnalysisError when the topic is missing."""
    cleaned = {
        "topic": _clip(brief.get("topic")),
        "brand": _clip(brief.get("brand"), 200),
        "audience": _clip(brief.get("audience"), 300),
        "goal": _clip(brief.get("goal"), 300),
        "format": brief.get("format") if brief.get("format") in FORMATS else "video",
        "length_seconds": None,
        "platform": _clip(brief.get("platform"), 40),
        "style": _clip(brief.get("style")),
        "must_include": _clip(brief.get("must_include")),
    }
    try:
        seconds = int(brief.get("length_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if cleaned["format"] == "video" and seconds > 0:
        cleaned["length_seconds"] = max(5, min(90, seconds))
    if not cleaned["topic"]:
        raise AnalysisError("Say what your post is about first.")
    return cleaned


def audit_facts(report: dict) -> dict:
    """The parts of an audit worth sending: the reading, not the frames or the raw scrape."""
    post = report.get("post") or {}
    return {
        "platform": report.get("platform_label") or report.get("platform", ""),
        "media_type": post.get("media_type", ""),
        "duration_seconds": post.get("duration_seconds"),
        "caption": _clip(post.get("caption"), 1500),
        "metrics": report.get("metrics") or {},
        "analysis": report.get("analysis") or {},
    }


def build_content(report: dict, brief: dict) -> list[dict]:
    wanted = {k: v for k, v in brief.items() if v}
    return [
        {"type": "text", "text": "Audit of the post that performed well:\n"
                                 + json.dumps(audit_facts(report), indent=1, ensure_ascii=False)[:20000]},
        {"type": "text", "text": "Brief for our new post:\n" + json.dumps(wanted, indent=1, ensure_ascii=False)},
        {"type": "text", "text": "Write the Higgsfield prompt pack for our new post."},
    ]


def generate(report: dict, brief: dict, client: anthropic.Anthropic | None = None) -> tuple[dict, dict]:
    """Return (prompt pack, usage). The brief must have been through clean_brief."""
    if not (report.get("analysis") or {}).get("verdict"):
        raise AnalysisError("This audit has no analysis to build a prompt from.")
    pack, usage = call_claude(client or anthropic.Anthropic(), system=SYSTEM, content=build_content(report, brief),
                              schema=SCHEMA, effort=EFFORT, max_tokens=12000, what="write this prompt")
    pack["brief"] = brief
    pack["as_text"] = as_text(pack)
    return pack, usage


def as_text(pack: dict) -> str:
    """Everything in one block, ready to paste into a doc or a chat."""
    brief = pack.get("brief") or {}
    lines = [f"HIGGSFIELD PROMPT PACK — {brief.get('topic', '')}".rstrip(" —"), "",
             f"Concept: {pack.get('concept', '')}", f"Aspect ratio: {pack.get('aspect_ratio', '')}", "",
             "Style (keep the same in every shot):", pack.get("style_anchor", ""), ""]
    for i, shot in enumerate(pack.get("shots") or [], 1):
        head = f"SHOT {i} · {shot.get('label', '')}" + (f" · {shot['timing']}" if shot.get("timing") else "")
        lines += [head, f"Purpose: {shot.get('purpose', '')}", f"Start frame prompt: {shot.get('start_frame_prompt', '')}"]
        for key, name in (("video_prompt", "Video prompt"), ("camera_motion", "Camera"),
                          ("on_screen_text", "On-screen text"), ("voiceover", "Voiceover"), ("sound", "Sound")):
            if shot.get(key):
                lines.append(f"{name}: {shot[key]}")
        lines.append("")
    lines += ["Caption:", pack.get("caption", "")]
    if pack.get("hashtags"):
        lines.append(" ".join(t if t.startswith("#") else "#" + t for t in pack["hashtags"]))
    if pack.get("production_notes"):
        lines += ["", "Notes:"] + [f"- {n}" for n in pack["production_notes"]]
    return "\n".join(lines).strip() + "\n"
