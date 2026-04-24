#!/usr/bin/env bash
# Deprecated shim. Forwards to build_py2app.sh, which is the real builder.
# The original build_app.sh produced a bash-shim Sentrial.app that relied
# on the system Python.framework — that couldn't satisfy macOS TCC mic
# attribution. py2app embeds Python inside the bundle, which is what works.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/build_py2app.sh" "$@"
