"""
Pursuit Visuals — dark modern proposal template.

Visual register:
  - Near-black background with subtle gradient/grain
  - Bold display sans for headings (system Inter / Geist stack)
  - Vibrant orange-red accent (Pursuit's energy color) on the title bar +
    pricing block + CTAs
  - Monospaced metadata (client, date, deadline) for that production-shop
    feel — like reading a shot list
  - Plenty of negative space; no rounded marshmallow buttons

One-pager and major formats share the chrome; major adds a sticky-ish
table of contents in the side rail and pads the type up a notch.
"""
from __future__ import annotations

from sentrial.mcps.proposals.templates._shared import (
    esc, paragraphs, bullets, render_sections,
)


# Pursuit palette — kept here so designers can edit colors without touching
# the renderer logic.
PALETTE = {
    "bg":          "#0a0a0c",
    "bg_alt":      "#111114",
    "surface":     "#16161a",
    "border":      "rgba(255,255,255,0.08)",
    "text":        "#f4f4f5",
    "text_dim":    "#a1a1aa",
    "text_mute":   "#71717a",
    "accent":      "#ff5a3c",   # Pursuit orange-red
    "accent_glow": "rgba(255, 90, 60, 0.35)",
    "mono_accent": "#ffd166",   # ochre for metadata accents
}


def _styles(format: str) -> str:
    """Format-aware CSS. one_pager keeps things tight; major loosens the
    leading + adds a left rail."""
    container_max = "920px" if format == "major" else "760px"
    body_size = "16.5px" if format == "major" else "16px"
    leading = "1.65" if format == "major" else "1.6"
    return f"""
    :root {{
      --bg: {PALETTE['bg']};
      --bg-alt: {PALETTE['bg_alt']};
      --surface: {PALETTE['surface']};
      --border: {PALETTE['border']};
      --text: {PALETTE['text']};
      --text-dim: {PALETTE['text_dim']};
      --text-mute: {PALETTE['text_mute']};
      --accent: {PALETTE['accent']};
      --accent-glow: {PALETTE['accent_glow']};
      --mono: 'JetBrains Mono', 'SF Mono', Menlo, monospace;
      --sans: 'Inter', 'Geist', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      background-image:
        radial-gradient(circle at 10% -10%, rgba(255, 90, 60, 0.12), transparent 50%),
        radial-gradient(circle at 90% 110%, rgba(255, 90, 60, 0.06), transparent 55%);
      color: var(--text);
      font-family: var(--sans);
      font-size: {body_size};
      line-height: {leading};
      -webkit-font-smoothing: antialiased;
      letter-spacing: -0.005em;
    }}
    .prop-page {{
      max-width: {container_max};
      margin: 0 auto;
      padding: 80px 56px 120px;
    }}
    .prop-meta {{
      display: flex; gap: 24px; flex-wrap: wrap;
      font-family: var(--mono); font-size: 11px;
      letter-spacing: 0.12em; text-transform: uppercase;
      color: var(--text-mute);
      margin-bottom: 36px;
    }}
    .prop-meta strong {{ color: {PALETTE['mono_accent']}; font-weight: 500; }}
    .prop-brand {{
      font-family: var(--mono); font-size: 12px;
      letter-spacing: 0.32em; text-transform: uppercase;
      color: var(--accent);
      margin-bottom: 8px;
    }}
    .prop-title {{
      font-size: clamp(40px, 6vw, 64px);
      font-weight: 700; letter-spacing: -0.03em; line-height: 1.05;
      margin: 0 0 16px;
      background: linear-gradient(180deg, #fff 30%, #c4c4c8 100%);
      -webkit-background-clip: text; background-clip: text;
      color: transparent;
    }}
    .prop-subtitle {{
      font-size: clamp(17px, 2.2vw, 21px);
      color: var(--text-dim);
      max-width: 36em;
      margin: 0 0 56px;
    }}
    .prop-intro p {{
      color: var(--text);
      margin: 0 0 1.1em;
    }}
    .prop-intro {{ margin-bottom: 56px; }}
    .prop-section {{
      margin: 64px 0;
      padding-top: 24px;
      border-top: 1px solid var(--border);
    }}
    .prop-section h2 {{
      font-size: 22px; font-weight: 600; letter-spacing: -0.01em;
      margin: 0 0 16px;
      color: var(--text);
    }}
    .prop-section h2::before {{
      content: ""; display: inline-block;
      width: 6px; height: 14px;
      background: var(--accent);
      margin-right: 12px;
      transform: translateY(1px);
    }}
    .prop-section p {{ margin: 0 0 1em; color: var(--text-dim); }}
    .prop-section p:last-child {{ margin-bottom: 0; }}
    .prop-section ul {{
      list-style: none; padding: 0; margin: 18px 0 0;
    }}
    .prop-section li {{
      padding: 10px 0 10px 20px;
      border-bottom: 1px solid var(--border);
      color: var(--text);
      position: relative;
    }}
    .prop-section li::before {{
      content: "→";
      color: var(--accent);
      position: absolute; left: 0;
    }}
    .prop-section li:last-child {{ border-bottom: none; }}
    .prop-pricing {{
      margin: 72px 0 48px;
      padding: 36px 40px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-left: 3px solid var(--accent);
    }}
    .prop-pricing h2 {{
      font-family: var(--mono);
      font-size: 11px; letter-spacing: 0.32em; text-transform: uppercase;
      color: var(--accent); margin: 0 0 14px;
    }}
    .prop-pricing p {{ margin: 0 0 0.8em; color: var(--text); }}
    .prop-pricing p:last-child {{ margin-bottom: 0; }}
    .prop-timeline {{
      margin: 48px 0;
      padding: 28px 32px;
      border: 1px solid var(--border);
      background: var(--bg-alt);
    }}
    .prop-timeline h2 {{
      font-family: var(--mono);
      font-size: 11px; letter-spacing: 0.32em; text-transform: uppercase;
      color: var(--text-mute); margin: 0 0 14px;
    }}
    .prop-timeline p {{ margin: 0 0 0.8em; color: var(--text-dim); }}
    .prop-timeline p:last-child {{ margin-bottom: 0; }}
    .prop-cta {{
      margin: 64px 0 0;
      padding: 32px 0;
      border-top: 1px solid var(--border);
      color: var(--text);
      font-size: 17px;
    }}
    .prop-cta strong {{ color: var(--accent); }}
    .prop-foot {{
      margin-top: 96px;
      padding-top: 24px;
      border-top: 1px solid var(--border);
      font-family: var(--mono);
      font-size: 10px; letter-spacing: 0.24em; text-transform: uppercase;
      color: var(--text-mute);
      display: flex; justify-content: space-between; gap: 24px;
    }}
    @media (max-width: 640px) {{
      .prop-page {{ padding: 56px 24px 80px; }}
      .prop-pricing, .prop-timeline {{ padding: 24px; }}
      .prop-meta {{ gap: 16px; }}
    }}
    @media print {{
      body {{ background: white; color: #0a0a0c; }}
      .prop-page {{ padding: 32px; }}
      .prop-title {{ -webkit-text-fill-color: initial; color: #0a0a0c; background: none; }}
      .prop-section {{ break-inside: avoid; }}
    }}
    """


def render(structured: dict, brief) -> str:
    meta = structured.get("_meta") or {}
    title = esc(structured.get("title") or "Proposal")
    subtitle = esc(structured.get("subtitle") or "")
    intro = paragraphs(structured.get("intro"))
    sections = render_sections(structured.get("sections"))
    pricing = paragraphs(structured.get("pricing"))
    timeline = paragraphs(structured.get("timeline"))
    next_steps = esc(structured.get("next_steps") or "")
    client = esc(meta.get("client") or brief.client)
    date = esc(meta.get("generated_at") or "")
    deadline = esc(meta.get("deadline") or "")

    meta_pieces = [f"For <strong>{client}</strong>"]
    if date:
        meta_pieces.append(f"Issued <strong>{date}</strong>")
    if deadline:
        meta_pieces.append(f"Decision by <strong>{deadline}</strong>")
    meta_html = "  ".join(f"<span>{p}</span>" for p in meta_pieces)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Pursuit Visuals × {client}</title>
<style>{_styles(brief.format)}</style>
</head>
<body>
<main class="prop-page">
  <div class="prop-meta">{meta_html}</div>
  <div class="prop-brand">Pursuit Visuals</div>
  <h1 class="prop-title">{title}</h1>
  <p class="prop-subtitle">{subtitle}</p>
  <div class="prop-intro">
{intro}
  </div>
{sections}
  <div class="prop-pricing">
    <h2>Investment</h2>
{pricing}
  </div>
  <div class="prop-timeline">
    <h2>Timeline</h2>
{timeline}
  </div>
  <div class="prop-cta">
    <strong>Next:</strong> {next_steps}
  </div>
  <div class="prop-foot">
    <span>Pursuit Visuals</span>
    <span>{client}</span>
    <span>{date}</span>
  </div>
</main>
</body>
</html>"""
