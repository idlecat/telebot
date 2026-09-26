#!/bin/sh
# Example handler invoked as: handle.sh "<full message text>"
# Replace this with whatever you actually want the bot to run.
set -eu

msg="$1"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/downloads}"

case "$msg" in
  ping)
    echo "pong"
    ;;
  status)
    uptime
    df -h
    ;;
  http://*|https://*)
    mkdir -p "$DOWNLOAD_DIR"
    yt-dlp \
      --no-progress \
      --restrict-filenames \
      -x --audio-format aac --audio-quality 0 \
      --paths "$DOWNLOAD_DIR" \
      -o "%(title)s [%(id)s].%(ext)s" \
      --print "after_move:Saved: %(filepath)s" \
      -- "$msg"
    ;;
  *)
    echo "Unknown command: $msg"
    exit 1
    ;;
esac
