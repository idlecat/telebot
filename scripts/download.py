#!/usr/bin/env python3
"""Download a link -- a single video or a whole playlist -- as audio, then
for each song: tag it (backfill_metadata), drop it if the library already has
it, and otherwise move it into the artist's own folder if one exists.

Usage: download.py "URL [info]"

The argument is the whole Telegram message: the link, optionally followed by
what you know about the song, which then overrides automatic identification:

    https://youtu.be/xxxx 邓紫棋 - 泡沫       artist and title
    https://youtu.be/xxxx 邓紫棋              artist only
    https://youtu.be/xxxx
    artist: 邓紫棋
    title: 泡沫
    album: Xposed                            one "key: value" per line

On a playlist, a given title is ignored (it can't fit every song); artist and
album apply to all of them.

Prints one line per song as it finishes (telebot streams these back to
Telegram), then a summary. Two layers of dedup:

1. yt-dlp's download archive skips video IDs downloaded before. It's seeded
   with every "[<id>]" already in a library filename, so songs fetched before
   the archive existed are skipped too.
2. After tagging, a song whose artist + title + length match a file already
   in the library (a different upload of the same track) is deleted.

Env: MUSIC_DIR (library root, default /music), DOWNLOAD_DIR (where new files
land and stay when no artist folder matches, default $MUSIC_DIR/others),
MAX_PLAYLIST_ITEMS (default 200), ACOUSTID_API_KEY (see backfill_metadata).
"""
import os
import re
import shutil
import sys

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor

import backfill_metadata
from library import Library, audio_duration

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR") or os.path.join(MUSIC_DIR, "others")
MAX_ITEMS = int(os.environ.get("MAX_PLAYLIST_ITEMS", "200"))
STATE_DIR = os.path.join(MUSIC_DIR, ".telebot")
ARCHIVE = os.path.join(STATE_DIR, "download-archive.txt")
ARCHIVED = " has already been recorded in the archive"


def say(msg):
    print(msg, flush=True)


class Stats:
    added = duplicates = archived = failed = 0


class Logger:
    """yt-dlp routes all its screen output here; surface only what matters."""

    def __init__(self, stats):
        self.stats = stats

    def debug(self, msg):
        if msg.endswith(ARCHIVED):
            self.stats.archived += 1
            say(f"⏭ Already downloaded: {msg.removeprefix('[download] ').removesuffix(ARCHIVED)}")
        elif msg.startswith("[download] Downloading playlist:"):
            say(f"▶ Playlist: {msg.split(':', 1)[1].strip()}")
        elif m := re.search(r"Playlist .*: Downloading (\d+) items", msg):
            say(f"▶ {m.group(1)} items")

    info = debug

    def warning(self, msg):
        pass

    def error(self, msg):
        self.stats.failed += 1
        say(f"✗ {msg.removeprefix('ERROR: ')}")


class FileIntoLibrary(PostProcessor):
    def __init__(self, library, api_key, stats, given):
        super().__init__()
        self.library = library
        self.api_key = api_key
        self.stats = stats
        self.given = given

    def run(self, info):
        path = info["filepath"]
        try:
            given = dict(self.given)
            if (info.get("n_entries") or 1) > 1:
                given.pop("title", None)
            line, new_path = self.handle(path, site_metadata(info), given)
            info["filepath"] = new_path or path
        except Exception as e:
            self.stats.failed += 1
            line = f"✗ {os.path.basename(path)}: {e}"
        say(line)
        return [], info

    def handle(self, path, hint, given):
        lib = self.library
        line, artist, title = backfill_metadata.identify_and_tag(path, self.api_key, hint, given)
        if artist and title:
            dup = lib.find_duplicate(artist, title, audio_duration(path), exclude=path)
            if dup:
                os.remove(path)
                self.stats.duplicates += 1
                return f"⏭ Duplicate of {dup} ({artist} - {title}); new copy removed", None
            dest_dir = lib.artist_dir(artist)
            if dest_dir and os.path.dirname(path) != dest_dir:
                dest = os.path.join(dest_dir, os.path.basename(path))
                if os.path.exists(dest):
                    line += f" (not moved: {os.path.relpath(dest, MUSIC_DIR)} exists)"
                else:
                    shutil.move(path, dest)
                    path = dest
                    line += f" → {os.path.relpath(dest_dir, MUSIC_DIR)}/"
        lib.add(path)
        self.stats.added += 1
        return line, path


INFO_KEYS = {
    "artist": "artist", "singer": "artist", "歌手": "artist", "艺人": "artist",
    "title": "title", "song": "title", "歌名": "title", "歌曲": "title",
    "album": "album", "专辑": "album",
}


def parse_message(message):
    """'<url> [info]' -> (url, {"artist", "title", "album"}). Info is either
    'key: value' lines, or free text: 'Artist - Title', or just 'Artist'."""
    parts = message.strip().split(None, 1)
    url = parts[0] if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    given = {}
    free = []
    for line in rest.splitlines():
        m = re.match(r"\s*([^:：=]+?)\s*[:：=]\s*(.+)$", line)
        key = INFO_KEYS.get(m.group(1).lower()) if m else None
        if key:
            given[key] = m.group(2).strip()
        elif line.strip():
            free.append(line.strip())
    if free:
        text = " ".join(free)
        if " - " in text:
            given.setdefault("artist", text.split(" - ", 1)[0].strip())
            given.setdefault("title", text.split(" - ", 1)[1].strip())
        else:
            given.setdefault("artist", text)
    return url, {k: v for k, v in given.items() if v}


def site_metadata(info):
    """Real track metadata the site exposes, e.g. YouTube Music's
    auto-generated "Provided to YouTube" uploads -- not the video title."""
    artists = info.get("artists") or ([info["artist"]] if info.get("artist") else [])
    return {
        "artist": ", ".join(dict.fromkeys(artists)) or None,
        "title": info.get("track"),
        "album": info.get("album"),
    }


def seed_archive(library):
    """Add library files' YouTube IDs to the archive (one-time catch-up for
    files that predate it; cheap enough to just do every run)."""
    try:
        with open(ARCHIVE, encoding="utf-8") as f:
            known = {line.strip() for line in f}
    except OSError:
        known = set()
    missing = {f"youtube {i}" for i in library.youtube_ids()} - known
    if missing:
        with open(ARCHIVE, "a", encoding="utf-8") as f:
            f.writelines(f"{line}\n" for line in sorted(missing))


def main():
    if len(sys.argv) != 2:
        print('usage: download.py "URL [info]"', file=sys.stderr)
        sys.exit(1)
    url, given = parse_message(sys.argv[1])
    if given:
        say("ℹ Using " + ", ".join(f"{k}: {v}" for k, v in given.items()))
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)

    library = Library(MUSIC_DIR, DOWNLOAD_DIR, STATE_DIR)
    seed_archive(library)

    stats = Stats()
    opts = {
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "aac",
            "preferredquality": "0",
        }],
        "paths": {"home": DOWNLOAD_DIR},
        "outtmpl": "%(title)s [%(id)s].%(ext)s",
        "download_archive": ARCHIVE,
        "ignoreerrors": True,  # one unavailable playlist item shouldn't stop the rest
        "playlistend": MAX_ITEMS,
        "noprogress": True,
        "logger": Logger(stats),
    }
    api_key = os.environ.get("ACOUSTID_API_KEY", "").strip()
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(FileIntoLibrary(library, api_key, stats, given), when="after_move")
        ydl.download([url])

    parts = [f"{stats.added} added"]
    if stats.duplicates:
        parts.append(f"{stats.duplicates} duplicate(s) removed")
    if stats.archived:
        parts.append(f"{stats.archived} already downloaded")
    if stats.failed:
        parts.append(f"{stats.failed} failed")
    say("Done: " + ", ".join(parts))
    sys.exit(1 if stats.failed and not stats.added else 0)


if __name__ == "__main__":
    main()
