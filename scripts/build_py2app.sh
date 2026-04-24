#!/usr/bin/env bash
# Build a self-contained Sentrial.app bundle via py2app.
#
# This replaces the old shell-shim Sentrial.app (which relied on the system
# Python.app whose Info.plist lacks mic usage keys). The py2app bundle
# embeds its own Python interpreter at
#   Sentrial.app/Contents/Frameworks/Python.framework/Versions/<v>/Python
# so mic requests get attributed to this bundle, not to /Library/Frameworks.
#
# Output: ./Sentrial.app at the repo root.
# Idempotent. Re-run any time sentrial/ code changes.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "$here"
app="$here/Sentrial.app"

if [[ ! -x "./.venv/bin/python" ]]; then
    echo "== .venv missing. Run: python3 -m venv .venv && ./.venv/bin/pip install -e ."
    exit 1
fi

# Ensure py2app is available in the venv. Pin setuptools to <70 because
# py2app 0.28 uses setuptools.installer which setuptools 70+ removed, and
# the stdlib distutils is gone in Python 3.12+ so we need setuptools'
# compat shim (that shim exists in 69.x but not all later builds).
./.venv/bin/python -c "import py2app, distutils.core" 2>/dev/null || {
    echo "-> Installing py2app + setuptools<70"
    ./.venv/bin/pip install py2app 'setuptools<70' >/dev/null
}

# Compile the native Swift mic helper. Staged in /tmp (NOT ./build, which
# py2app wipes mid-run) so we can drop it into the final bundle afterwards.
MIC_STAGING="$(mktemp -d -t sentrial-mic-build)"
trap 'rm -rf "$MIC_STAGING"' EXIT
if [[ -f "$here/native/sentrial-mic.swift" ]]; then
    echo "-> Compiling native/sentrial-mic.swift (with embedded Info.plist)"
    mic_plist="$MIC_STAGING/Info.plist"
    cat > "$mic_plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>com.sentrial.mic</string>
    <key>CFBundleName</key><string>Sentrial</string>
    <key>CFBundleExecutable</key><string>sentrial-mic</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Sentrial uses the microphone for Voice Mode so you can speak with the assistant.</string>
</dict>
</plist>
PLIST
    swiftc -O -target arm64-apple-macos11 \
        "$here/native/sentrial-mic.swift" \
        -o "$MIC_STAGING/sentrial-mic" \
        -framework AVFoundation -framework Foundation \
        -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$mic_plist"
fi

# Clean prior build.
rm -rf "$app" "$here/build" "$here/dist"

echo "-> Running py2app (arm64)"
./.venv/bin/python setup_py2app.py py2app --arch=arm64 --no-strip 2>&1 | \
    grep -v "^  " | grep -v "^compiling" | tail -20 || true

# py2app writes to ./dist/Sentrial.app — move it to the repo root for
# launchd + IDE discoverability.
if [[ -d "$here/dist/Sentrial.app" ]]; then
    rm -rf "$app"
    mv "$here/dist/Sentrial.app" "$app"
    rm -rf "$here/dist"
fi

# Drop the compiled Swift mic helper into the bundle.
if [[ -f "$MIC_STAGING/sentrial-mic" ]]; then
    cp "$MIC_STAGING/sentrial-mic" "$app/Contents/MacOS/sentrial-mic"
    chmod +x "$app/Contents/MacOS/sentrial-mic"
fi

# Strip xattrs (swiftc + cp both add them; codesign rejects otherwise).
xattr -cr "$app"

# Ad-hoc codesign the whole bundle. py2app auto-signs on ARM64 but
# inserting sentrial-mic afterwards invalidates the signature — so we
# re-sign deep at the end.
echo "-> Ad-hoc codesign"
codesign --force --sign - --deep "$app" 2>&1 | sed 's/^/   /' || true

echo
echo "== Built: $app"
echo "   Bundle ID:  $(defaults read "$app/Contents/Info" CFBundleIdentifier 2>/dev/null || echo '?')"
echo "   Mic key:    $(defaults read "$app/Contents/Info" NSMicrophoneUsageDescription 2>/dev/null | head -c 60)…"
echo "   Verify:     codesign -dv '$app'"
echo
echo "To test: open '$app' — first Voice Mode trigger should prompt for mic."
