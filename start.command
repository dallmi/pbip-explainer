#!/bin/sh
# pbip-explainer - start the local web app (macOS / Linux)
cd "$(dirname "$0")" || exit 1
exec python3 app/server.py "$@"
