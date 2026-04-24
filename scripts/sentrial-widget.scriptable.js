// Sentrial — iOS home-screen widget (Scriptable app)
//
// Install:
//   1. Install "Scriptable" from the App Store (free).
//   2. Create a new script, paste this whole file.
//   3. Set URL + TOKEN below (long-press on the script to edit).
//   4. On your home screen: long-press → + → Scriptable → pick this script → add widget.
//   5. Tap the widget to jump into the Sentrial PWA.
//
// Widget sizes supported: small, medium. Large shows audit stream.

const URL   = "https://YOUR-RAILWAY-URL.up.railway.app";
const TOKEN = "PASTE_YOUR_SENTRIAL_TOKEN";

// ---- fetch state ----
async function fetchState() {
  const req = new Request(`${URL}/api/state`);
  req.headers = { "Authorization": `Bearer ${TOKEN}` };
  try { return await req.loadJSON(); } catch { return null; }
}

function hex(h) { return new Color(h); }
const BG = hex("#0a0a0f");
const SURFACE = hex("#12131a");
const ACCENT = hex("#7dd3fc");
const AMBER  = hex("#fbbf24");
const TEXT   = hex("#e5e7eb");
const DIM    = hex("#6b7280");
const GREEN  = hex("#4ade80");

function makeWidget(state) {
  const w = new ListWidget();
  w.backgroundColor = BG;
  w.url = `${URL}/ui/`;
  w.setPadding(14, 14, 14, 14);

  // Header
  const h = w.addStack();
  h.centerAlignContent();
  const dot = h.addText("●");
  dot.textColor = state ? GREEN : hex("#f87171");
  dot.font = Font.systemFont(10);
  h.addSpacer(6);
  const title = h.addText("SENTRIAL");
  title.textColor = TEXT;
  title.font = Font.semiboldMonospacedSystemFont(11);
  h.addSpacer();
  const clock = h.addText(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  clock.textColor = DIM;
  clock.font = Font.regularMonospacedSystemFont(10);

  w.addSpacer(10);

  if (!state) {
    const err = w.addText("offline");
    err.textColor = DIM;
    err.font = Font.systemFont(12);
    return w;
  }

  // Active jobs count
  const pending = (state.jobs || []).filter(j => j.status === "pending_approval").length;
  const running = (state.jobs || []).filter(j => j.status === "running").length;

  const row = w.addStack();
  row.layoutHorizontally();
  row.spacing = 10;

  // pending card
  const c1 = row.addStack();
  c1.layoutVertically();
  c1.backgroundColor = SURFACE;
  c1.cornerRadius = 8;
  c1.setPadding(8, 10, 8, 10);
  const l1 = c1.addText("PENDING");
  l1.textColor = DIM;
  l1.font = Font.regularMonospacedSystemFont(9);
  const v1 = c1.addText(String(pending));
  v1.textColor = pending > 0 ? AMBER : TEXT;
  v1.font = Font.semiboldSystemFont(22);

  // running card
  const c2 = row.addStack();
  c2.layoutVertically();
  c2.backgroundColor = SURFACE;
  c2.cornerRadius = 8;
  c2.setPadding(8, 10, 8, 10);
  const l2 = c2.addText("RUNNING");
  l2.textColor = DIM;
  l2.font = Font.regularMonospacedSystemFont(9);
  const v2 = c2.addText(String(running));
  v2.textColor = running > 0 ? ACCENT : TEXT;
  v2.font = Font.semiboldSystemFont(22);

  // Latest audit — only in medium/large widgets
  const size = config.widgetFamily || "medium";
  if (size !== "small" && state.audit && state.audit.length) {
    w.addSpacer(10);
    const latest = state.audit[0];
    const a = w.addText(latest.action || "");
    a.textColor = TEXT;
    a.font = Font.regularMonospacedSystemFont(10);
    a.lineLimit = 2;
    const t = w.addText((latest.timestamp || "").slice(11, 19));
    t.textColor = DIM;
    t.font = Font.regularMonospacedSystemFont(9);
  }

  return w;
}

const state = await fetchState();
const widget = makeWidget(state);

if (!config.runsInWidget) {
  await widget.presentMedium();
}

Script.setWidget(widget);
Script.complete();
