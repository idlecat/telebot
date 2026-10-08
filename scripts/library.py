"""Music-library helpers: artist-name canonicalisation, artist-folder lookup,
and a cached tag index used to spot duplicate songs.

The library is laid out with one top-level folder per artist (e.g. 邓紫棋/,
TaylorSwift/) next to catch-all folders (others/, 90s_Cantopop/, ...). A
download is filed into an artist's folder only if one already exists --
nothing here creates new artist folders.
"""
import hashlib
import json
import os
import re
import unicodedata

from mutagen import File as MutagenFile

AUDIO_EXTS = (".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".wav", ".wma", ".ape")
ALIASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artist_aliases.json")
# Splits a multi-artist credit: "A, B", "A / B", "A & B", "A feat. B", "A x B".
# Only used for matching -- a band name like "AC/DC" or "Simon & Garfunkel"
# splits too, so the whole name is always tried first and tags keep it intact.
ARTIST_SPLIT = re.compile(
    r"(\s*(?:[,/&、;＆，⧸]|\s(?:feat\.?|ft\.?|featuring|x|×)\s)\s*)", re.IGNORECASE
)
INDEX_VERSION = 2


def _index_stamp():
    """Cached index entries are only valid for this code version and this
    alias file -- the stored artist keys are alias-mapped, so an edit to
    artist_aliases.json must force a re-read of every file's tags."""
    try:
        with open(ALIASES_PATH, "rb") as f:
            digest = hashlib.sha1(f.read()).hexdigest()
    except OSError:
        digest = "none"
    return f"{INDEX_VERSION}:{digest}"
CJK_RUN = re.compile(r"[一-鿿]+")
LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z .'\-]*[A-Za-z.]|[A-Za-z]")
YTID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.\w+$")
NORM_DROP = re.compile(r"[/&+＆⧸\s.·・'’\"`\-_()（）\[\]!！?？]+")


def norm(s):
    """Comparison key: 'Taylor Swift' == 'TaylorSwift', 'Céline Dion' == 'CelineDion'."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return NORM_DROP.sub("", s).lower()


def split_artists(artist):
    return [p.strip() for p in ARTIST_SPLIT.split(artist or "")[::2] if p.strip()]


def load_raw_aliases():
    try:
        with open(ALIASES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _load_aliases():
    return {norm(k): v for k, v in load_raw_aliases().items()}


ALIASES = _load_aliases()


def name_halves(part):
    """'王傑 Dave Wang' -> ['王傑', 'Dave Wang']: the Chinese and Latin halves of
    a name that has both, or [] if it doesn't."""
    cjk = CJK_RUN.findall(part)
    latin = [l.strip() for l in LATIN_RUN.findall(part)]
    return ["".join(cjk)] + latin if cjk and latin else []


def canonical_part(part):
    """'G.E.M.' -> '邓紫棋'; '王傑 Dave Wang' -> '王杰' (one half is a known
    alias). Names with no alias are returned unchanged -- a mixed-script
    name like 'TF家族' is one name, not a Chinese name plus an English one."""
    for candidate in [part] + name_halves(part):
        if norm(candidate) in ALIASES:
            return ALIASES[norm(candidate)]
    return part


def audio_duration(path):
    try:
        f = MutagenFile(path)
        return round(f.info.length, 1) if f is not None and f.info else None
    except Exception:
        return None


def canonical_artist(artist):
    """Map each credited artist through the aliases, keeping the original
    separators ('A & B' stays 'A & B') and dropping repeats that the mapping
    creates ('G.E.M. & 邓紫棋' -> '邓紫棋')."""
    if not artist:
        return artist
    if norm(artist) in ALIASES:
        return ALIASES[norm(artist)]
    pieces = ARTIST_SPLIT.split(artist)  # [part, sep, part, sep, ...]
    out, seen = [], set()
    for i in range(0, len(pieces), 2):
        part = pieces[i].strip()
        if not part:
            continue
        name = canonical_part(part)
        if norm(name) in seen:
            continue
        seen.add(norm(name))
        if out:
            out.append(pieces[i - 1])
        out.append(name)
    return "".join(out) or artist


def artist_keys(artist):
    """Match keys for a credit: the whole (canonical) name plus each artist in it."""
    canonical = canonical_artist(artist)
    return {norm(canonical)} | {norm(p) for p in split_artists(canonical)}


class Library:
    def __init__(self, root, download_dir, state_dir):
        self.root = os.path.abspath(root)
        self.download_dir = os.path.abspath(download_dir)
        self.index_path = os.path.join(state_dir, "index.json")
        self.entries = {}
        self.artist_dirs = {}
        for d in os.listdir(self.root):
            full = os.path.join(self.root, d)
            if d.startswith(".") or not os.path.isdir(full) or full == self.download_dir:
                continue
            self.artist_dirs[norm(d)] = full
        self.refresh()

    # ── index ────────────────────────────────────────────────────────────────
    def refresh(self):
        """Walk the library, re-reading tags only for files whose mtime/size
        changed since the cached index was written (the first run reads all)."""
        try:
            with open(self.index_path, encoding="utf-8") as f:
                data = json.load(f)
            old = data["entries"] if data.get("version") == _index_stamp() else {}
        except (OSError, ValueError, KeyError, AttributeError):
            old = {}
        new = {}
        for dirpath, dirnames, files in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in files:
                if not name.lower().endswith(AUDIO_EXTS):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, self.root)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                e = old.get(rel)
                if e and e.get("mtime") == st.st_mtime and e.get("size") == st.st_size:
                    new[rel] = e
                else:
                    new[rel] = self._read_entry(path, st)
        self.entries = new
        self.save()

    def _read_entry(self, path, st):
        artist = title = duration = None
        try:
            f = MutagenFile(path, easy=True)
            if f is not None:
                if f.tags:
                    artist = ", ".join(f.tags.get("artist", [])) or None
                    title = (f.tags.get("title") or [None])[0]
                duration = round(f.info.length, 1) if f.info else None
        except Exception:
            pass  # unreadable/corrupt file: index it with no tags
        return {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "a": sorted(artist_keys(artist)) if artist else [],
            "t": norm(title) if title else "",
            "d": duration,
        }

    def add(self, path):
        rel = os.path.relpath(path, self.root)
        self.entries[rel] = self._read_entry(path, os.stat(path))
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.index_path), exist_ok=True)
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": _index_stamp(), "entries": self.entries}, f, ensure_ascii=False)
        os.replace(tmp, self.index_path)

    # ── queries ──────────────────────────────────────────────────────────────
    def youtube_ids(self):
        """Video IDs of files already in the library, from yt-dlp's
        '<title> [<id>].<ext>' naming -- used to seed the download archive."""
        for rel in self.entries:
            m = YTID_IN_NAME.search(rel)
            if m:
                yield m.group(1)

    def artist_dir(self, artist):
        """Existing top-level folder for the whole credit ('AC/DC'), else the
        first credited artist that has one, else the first whose Chinese or
        Latin half does ('Eric 周兴哲' -> 周兴哲/)."""
        canonical = canonical_artist(artist)
        parts = split_artists(canonical)
        candidates = [canonical] + parts + [h for p in parts for h in name_halves(p)]
        for name in candidates:
            d = self.artist_dirs.get(norm(name))
            if d:
                return d
        return None

    def find_duplicate(self, artist, title, duration, exclude=None):
        """A library file with the same title, an overlapping artist, and a
        similar length (so a live/extended cut with the same title isn't
        mistaken for the studio track). Returns its relative path or None."""
        tkey = norm(title)
        akeys = artist_keys(artist)
        if not tkey or not akeys:
            return None
        exclude_rel = os.path.relpath(exclude, self.root) if exclude else None
        for rel, e in self.entries.items():
            if rel == exclude_rel or e.get("t") != tkey or not akeys & set(e.get("a", [])):
                continue
            d = e.get("d")
            if duration and d and abs(d - duration) > max(10, 0.06 * duration):
                continue
            return rel
        return None
