"""
Native proposal generator. Replaces the previous subprocess-based path
(`claude` CLI + a hardcoded Mac-only skills folder) so proposals actually
run on Railway, not just on Liam's laptop.

Pipeline:
  1. Receive a brief — client name + free-form request + optional pricing/deadline.
  2. Ask Claude (Sonnet 4.6) to produce structured JSON:
       title, subtitle, intro, sections[], pricing, timeline, next_steps.
     Sonnet because the output is meaningful long-form content; Haiku
     would skimp on the prose.
  3. Render that structure through a brand template (Pursuit dark or
     Sentrial light) at the requested size (one_pager or major).
  4. Save HTML to deliverables/<job_id>/proposal.html and a sidecar
     proposal.json with the structured data so we can re-render later
     into a different brand/format without re-asking the LLM.

Public API:
  generate(brief: ProposalBrief) -> ProposalResult
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic

from sentrial.core import secrets
from sentrial.mcps.proposals.templates import pursuit, sentrial

log = logging.getLogger(__name__)

# Sonnet 4.6 — proposal text is the actual deliverable, not a quick voice
# reply. Worth the extra latency vs Haiku.
GENERATOR_MODEL = "claude-sonnet-4-6"

VALID_BRANDS = {"pursuit", "sentrial"}
VALID_FORMATS = {"one_pager", "major"}


@dataclass
class ProposalBrief:
    """Input to the generator. Only `client` and `brief` are strictly required;
    everything else lets the generator skip a clarifying round-trip."""
    client: str                       # who the proposal is FOR (company / person)
    brief: str                        # the actual ask, free-form (transcript, notes, anything)
    brand: str = "sentrial"           # "pursuit" | "sentrial"
    format: str = "major"             # "one_pager" | "major"
    pricing_hint: str | None = None
    deadline: str | None = None
    extra_context: str | None = None  # optional notes / past relationship / tone

    def normalize(self) -> "ProposalBrief":
        b = (self.brand or "sentrial").lower().strip()
        if b not in VALID_BRANDS:
            b = "sentrial"
        f = (self.format or "major").lower().strip().replace("-", "_")
        if f in {"onepager", "one_page", "single", "short"}:
            f = "one_pager"
        if f in {"full", "long", "detailed"}:
            f = "major"
        if f not in VALID_FORMATS:
            f = "major"
        return ProposalBrief(
            client=self.client.strip(),
            brief=self.brief.strip(),
            brand=b,
            format=f,
            pricing_hint=(self.pricing_hint or "").strip() or None,
            deadline=(self.deadline or "").strip() or None,
            extra_context=(self.extra_context or "").strip() or None,
        )


@dataclass
class ProposalResult:
    job_id: str
    html_path: Path                   # rendered proposal.html
    json_path: Path                   # structured data sidecar
    brand: str
    format: str
    title: str
    word_count: int


# ---- LLM prompt ----------------------------------------------------------

def _build_llm_prompt(brief: ProposalBrief) -> str:
    """One generation prompt that produces structured JSON. Brand-agnostic at
    the LLM layer — the renderer applies brand styling later."""

    if brief.format == "one_pager":
        size_rules = (
            "ONE-PAGER format. Tight and decisive. Total target ~400-600 words.\n"
            "Sections: 3-4 max. Each section 2-4 sentences. No long lists.\n"
            "This is the kind of proposal the recipient skims in 90 seconds."
        )
    else:
        size_rules = (
            "MAJOR proposal format. Full pitch. Total target ~1500-2200 words.\n"
            "Sections: 5-7. Each section can run a longer paragraph + a short bullet "
            "list where it earns its place. Concrete deliverables, milestones.\n"
            "This is the kind of proposal a $25k+ engagement justifies."
        )

    brand_voice = {
        "pursuit": (
            "Pursuit Visuals — Liam's media company (video / photo / content). "
            "Voice is bold, direct, results-focused. Confident without being smug. "
            "Talks in concrete production terms (shot lists, deliverables, lead "
            "times). No fluff."
        ),
        "sentrial": (
            "Sentrial — Liam's AI agency. Voice is precise, technical when warranted, "
            "outcomes-first. Frames work as systems and leverage, not just hours. "
            "Distinguishes between manual labor and automation. No fluff."
        ),
    }[brief.brand]

    return f"""You are drafting a sales proposal for one of Liam's companies.

Company / brand voice:
{brand_voice}

Recipient: {brief.client}

Liam's brief (verbatim — use as the source of truth, but reorganize for clarity):
{brief.brief}

{"Pricing direction Liam mentioned: " + brief.pricing_hint if brief.pricing_hint else ""}
{"Deadline / timeline: " + brief.deadline if brief.deadline else ""}
{"Additional context: " + brief.extra_context if brief.extra_context else ""}

Format rules:
{size_rules}

Output ONLY a JSON object, no prose around it:

{{
  "title": "short bold headline — 4-8 words",
  "subtitle": "one-line follow-on — what this is, who it's for",
  "intro": "1-2 paragraphs setting up the engagement and why now",
  "sections": [
    {{
      "heading": "section title",
      "body": "the section content (paragraphs)",
      "bullets": ["optional bullet 1", "..."]    // omit or empty array if not used
    }}
  ],
  "pricing": "investment block — short paragraph or itemized lines",
  "timeline": "delivery timeline — short paragraph or week-by-week",
  "next_steps": "1-2 sentences — what happens when {brief.client} says yes"
}}

Critical rules:
- Real specifics from the brief, not generic agency-speak.
- No filler intros ("In today's fast-paced world...").
- No "we are excited / thrilled / honored" sycophancy.
- The recipient's name appears naturally — once or twice, not every paragraph.
- Concrete deliverables. "Three 60-second videos" beats "video content."
- If the brief is thin, infer reasonable defaults ONCE — don't ask questions
  in the JSON. The proposal must be complete and presentable.
"""


def _parse_llm_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Strip ``` fences if the model wraps the JSON.
    if text.startswith("```"):
        # Drop opening fence (and optional language tag), then trailing fence.
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch: find first { and last } and try again.
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i:j + 1])
        raise


# ---- Render layer --------------------------------------------------------

def _render(structured: dict, brief: ProposalBrief) -> str:
    """Pick the right brand template + format and render. Brand modules expose
    `render(structured, brief)` returning a complete HTML document."""
    if brief.brand == "pursuit":
        return pursuit.render(structured, brief)
    return sentrial.render(structured, brief)


# ---- Public API ----------------------------------------------------------

async def generate(brief: ProposalBrief, out_dir: Path) -> ProposalResult:
    """Run the full pipeline. out_dir should be deliverables/<job_id>/ and
    the caller creates it. The generator writes proposal.html + proposal.json."""
    brief = brief.normalize()
    out_dir.mkdir(parents=True, exist_ok=True)

    api_key = secrets.require("anthropic_api_key")
    client = AsyncAnthropic(api_key=api_key)

    prompt = _build_llm_prompt(brief)
    log.info("proposal generator: model=%s brand=%s format=%s client=%s brief_len=%d",
             GENERATOR_MODEL, brief.brand, brief.format, brief.client, len(brief.brief))

    # max_tokens tuned for the format. one_pager fits in ~1200 tokens of JSON;
    # major can run to ~3500 with full sections + pricing detail.
    max_tokens = 1600 if brief.format == "one_pager" else 4000

    resp = await client.messages.create(
        model=GENERATOR_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    structured = _parse_llm_json(raw)

    # Augment the structured doc with metadata the templates may want.
    structured["_meta"] = {
        "client": brief.client,
        "brand": brief.brand,
        "format": brief.format,
        "generated_at": datetime.now().strftime("%B %d, %Y"),
        "deadline": brief.deadline,
        "pricing_hint": brief.pricing_hint,
    }

    html = _render(structured, brief)

    html_path = out_dir / "proposal.html"
    html_path.write_text(html, encoding="utf-8")
    json_path = out_dir / "proposal.json"
    json_path.write_text(json.dumps(structured, indent=2, ensure_ascii=False), encoding="utf-8")

    # Quick word count from the rendered text, not the HTML tags.
    plain = re.sub(r"<[^>]+>", " ", html)
    word_count = len([w for w in plain.split() if w.strip()])

    job_id = out_dir.name
    return ProposalResult(
        job_id=job_id,
        html_path=html_path,
        json_path=json_path,
        brand=brief.brand,
        format=brief.format,
        title=str(structured.get("title", "Proposal")),
        word_count=word_count,
    )
