#!/bin/sh
# Example handler invoked as: handle.sh "<full message text>"
# Replace this with whatever you actually want the bot to run.
set -eu

msg="$1"

case "$msg" in
  ping)
    echo "pong"
    ;;
  status)
    uptime
    df -h
    ;;
  http://*|https://*)
    # Single video or playlist: download, tag, dedup, file into artist folder.
    exec python3 -u "$(dirname "$0")/download.py" "$msg"
    ;;
  *)
    echo "Unknown command: $msg"
    exit 1
    ;;
esac
