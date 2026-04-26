"""
Sentrial — light modern proposal template.

Visual register:
  - Off-white background, dark slate text
  - Crisp Inter / Geist sans throughout
  - Teal-cyan accent (matches the menubar PWA's --accent: #7dd3fc) on
    section markers, pricing block, and CTA — calm/technical feel
  - Generous leading; almost editorial
  - Mono captions for metadata (issued / for / deadline) — same family
    rhythm as the Pursuit template but inverted palette

Same structure as the Pursuit template; only the palette + a few
small chrome decisions differ.
"""
from __future__ import annotations

from sentrial.mcps.proposals.templates._shared import (
    esc, paragraphs, bullets, render_sections,
)


PALETTE = {
    "bg":          "#fafaf7",
    "bg_alt":      "#f4f4ef",
    "surface":     "#ffffff",
    "border":      "rgba(15, 23, 42, 0.10)",
    "border_soft": "rgba(15, 23, 42, 0.06)",
    "text":        "#0f172a",
    "text_dim":    "#475569",
    "text_mute":   "#64748b",
    "accent":      "#0891b2",   # Sentrial cyan/teal — matches menubar accent
    "accent_soft": "rgba(8, 145, 178, 0.08)",
    "accent_warm": "#0d9488",
}


def _styles(format: str) -> str:
    container_max = "920px" if format == "major" else "760px"
    body_size = "16.5px" if format == "major" else "16px"
    leading = "1.7" if format == "major" else "1.62"
    return f"""
    :root {{
      --bg: {PALETTE['bg']};
      --bg-alt: {PALETTE['bg_alt']};
      --surface: {PALETTE['surface']};
      --border: {PALETTE['border']};
      --border-soft: {PALETTE['border_soft']};
      --text: {PALETTE['text']};
      --text-dim: {PALETTE['text_dim']};
      --text-mute: {PALETTE['text_mute']};
      --accent: {PALETTE['accent']};
      --accent-soft: {PALETTE['accent_soft']};
      --accent-warm: {PALETTE['accent_warm']};
      --mono: 'JetBrains Mono', 'SF Mono', Menlo, monospace;
      --sans: 'Inter', 'Geist', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
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
    .prop-meta strong {{ color: var(--accent); font-weight: 500; }}
    .prop-brand {{
      font-family: var(--mono); font-size: 12px;
      letter-spacing: 0.32em; text-transform: uppercase;
      color: var(--accent);
      margin-bottom: 8px;
    }}
    .prop-title {{
      font-size: clamp(40px, 6vw, 60px);
      font-weight: 600; letter-spacing: -0.025em; line-height: 1.08;
      margin: 0 0 16px;
      color: var(--text);
    }}
    .prop-subtitle {{
      font-size: clamp(17px, 2.2vw, 21px);
      color: var(--text-dim);
      max-width: 36em;
      margin: 0 0 56px;
    }}
    .prop-intro {{ margin-bottom: 56px; }}
    .prop-intro p {{ color: var(--text); margin: 0 0 1.1em; }}
    .prop-intro p:last-child {{ margin-bottom: 0; }}
    .prop-section {{
      margin: 64px 0;
      padding-top: 28px;
      border-top: 1px solid var(--border-soft);
    }}
    .prop-section h2 {{
      font-size: 22px; font-weight: 600; letter-spacing: -0.01em;
      margin: 0 0 16px;
      color: var(--text);
      display: flex; align-items: center; gap: 12px;
    }}
    .prop-section h2::before {{
      content: ""; display: inline-block;
      width: 24px; height: 2px;
      background: var(--accent);
    }}
    .prop-section p {{ margin: 0 0 1em; color: var(--text-dim); }}
    .prop-section p:last-child {{ margin-bottom: 0; }}
    .prop-section ul {{
      list-style: none; padding: 0; margin: 18px 0 0;
    }}
    .prop-section li {{
      padding: 10px 0 10px 24px;
      border-bottom: 1px solid var(--border-soft);
      color: var(--text);
      position: relative;
    }}
    .prop-section li::before {{
      content: "";
      width: 8px; height: 8px;
      background: var(--accent);
      border-radius: 999px;
      position: absolute; left: 4px; top: 18px;
    }}
    .prop-section li:last-child {{ border-bottom: none; }}
    .prop-pricing {{
      margin: 72px 0 48px;
      padding: 36px 40px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      position: relative; overflow: hidden;
    }}
    .prop-pricing::before {{
      content: ""; position: absolute; left: 0; top: 0; bottom: 0;
      width: 4px; background: var(--accent);
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
      background: var(--bg-alt);
      border-radius: 12px;
      border: 1px solid var(--border-soft);
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
      border-top: 1px solid var(--border-soft);
      color: var(--text);
      font-size: 17px;
    }}
    .prop-cta strong {{ color: var(--accent); }}
    .prop-foot {{
      margin-top: 96px;
      padding-top: 24px;
      border-top: 1px solid var(--border-soft);
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
      .prop-page {{ padding: 32px; }}
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
<title>{title} — Sentrial × {client}</title>
<style>{_styles(brief.format)}</style>
</head>
<body>
<main class="prop-page">
  <div class="prop-meta">{meta_html}</div>
  <div class="prop-brand">Sentrial</div>
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
    <span>Sentrial</span>
    <span>{client}</span>
    <span>{date}</span>
  </div>
</main>
</body>
</html>"""
