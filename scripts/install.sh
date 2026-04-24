#!/usr/bin/env bash
# Sentrial — install script. Idempotent. Run from the repo root.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "$here"

echo "== Sentrial installer =="
echo "Repo:  $here"

# --- 1. Python venv --------------------------------------------------------
venv="$here/.venv"
if [[ ! -d "$venv" ]]; then
    echo "-> Creating venv at $venv"
    python3 -m venv "$venv"
fi
# shellcheck disable=SC1091
source "$venv/bin/activate"
python -m pip install --upgrade pip >/dev/null
echo "-> Installing Python deps (editable)"
python -m pip install -e .

# --- 2. Keychain setup -----------------------------------------------------
echo "-> Running first-run setup (Keychain prompts)"
python -m sentrial.core.daemon setup

# --- 3. App bundle (TCC host) ---------------------------------------------
# Python.app's Info.plist has no NSMicrophoneUsageDescription, so mic access
# is silently denied for anything running under python3. We wrap the menubar
# entrypoint in a Sentrial.app bundle whose Info.plist carries the required
# usage descriptions, and point launchd at that.
echo "-> Building Sentrial.app bundle"
"$here/scripts/build_app.sh"

# --- 4. Log dirs -----------------------------------------------------------
logs="$HOME/Library/Logs/Sentrial"
mkdir -p "$logs"

# --- 4. Install launchd plists --------------------------------------------
agents_dir="$HOME/Library/LaunchAgents"
mkdir -p "$agents_dir"

for plist in com.sentrial.daemon.plist com.sentrial.menubar.plist; do
    src="$here/scripts/$plist"
    dst="$agents_dir/$plist"
    echo "-> Installing $plist → $dst"
    sed \
        -e "s#__SENTRIAL_HOME__#$here#g" \
        -e "s#__SENTRIAL_VENV__#$venv#g" \
        -e "s#__SENTRIAL_LOGS__#$logs#g" \
        "$src" > "$dst"

    launchctl unload "$dst" 2>/dev/null || true
    launchctl load "$dst"
done

echo
echo "== Installed =="
echo "Daemon label:   com.sentrial.daemon"
echo "Menubar label:  com.sentrial.menubar"
echo "Logs:           $logs"
echo
echo "Next steps:"
echo "  - Click the 'Sentrial' menubar icon → 'Ask…' to send your first message."
echo "  - Grab the webhook token for iOS Shortcut:"
echo "      security find-generic-password -s com.sentrial.webhook_shared_secret -a sentrial -w"
echo "  - Tail audit log:       ./.venv/bin/sentrial audit-tail"
echo "  - Tail daemon output:   tail -f '$logs/daemon.out.log' '$logs/daemon.err.log'"
