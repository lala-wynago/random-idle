#!/bin/bash
# One-time setup: creates .venv next to this file and installs the packages the
# Lotto Simulator app needs. Double-click in Finder, or run it from Terminal.
set -e
cd "$(dirname "$0")"

PY=""
for candidate in \
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 \
    "$(command -v python3 || true)"; do
  if [ -x "$candidate" ] && "$candidate" -c 'import sys, tkinter; assert sys.version_info >= (3, 10)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "Couldn't find Python 3.10+ with Tk support."
  echo "Install it from https://www.python.org/downloads/macos/ and run this again."
  exit 1
fi

echo "Using $PY ($("$PY" --version))"
"$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements.txt
echo
echo "Setup complete. You can now open \"Lotto Simulator.app\"."
