#!/usr/bin/env python3
"""Identify a freshly-downloaded audio file and tag it.

Usage: backfill_metadata.py FILE [FILE ...]

Two-stage identification, same approach used for the one-off backfill of the
existing library:

1. Fingerprint the file with fpcalc and look it up against AcoustID (which
   resolves to MusicBrainz recording/artist/release data). Most accurate,
   but only works if this exact recording is already in AcoustID's
   crowdsourced database.
2. If that comes back empty (common for less-mainstream uploads, e.g. an
   artist's own channel re-upload that nobody's fingerprinted before), fall
   back to parsing the artist/title out of the yt-dlp video title itself --
   titles like "Artist -《Title》Official MV" or "Artist - Title" are common
   and carry everything needed; AcoustID coming up empty doesn't mean the
   filename is unusable.

Either way, any traditional-Chinese result is converted to simplified (so it
matches the rest of the library) before being written. Prints one summary
line per file to stdout, which telebot relays back to Telegram.

Requires ACOUSTID_API_KEY in the environment (free key from
https://acoustid.org/api-key) for stage 1; stage 2 needs nothing but the
filename. If a file can't be identified by either stage, it's left as
whatever yt-dlp wrote and a note is printed instead of a guess.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError

from library import canonical_artist, load_raw_aliases

ALIAS_NAMES = load_raw_aliases()  # "G.E.M." -> "邓紫棋", as written in the file

CONFIDENCE_THRESHOLD = 0.5
SEP = "\x01"


def fingerprint(path):
    try:
        r = subprocess.run(
            ["fpcalc", "-json", "-length", "120", path],
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, r.stderr.strip()
    return json.loads(r.stdout), None


def lookup(api_key, duration, fp):
    # Built manually: urlencode() would escape the literal "+" delimiters in
    # meta= into %2B, which the AcoustID server does not decode back,
    # silently dropping recordings/releasegroups data from the response.
    url = (
        "https://api.acoustid.org/v2/lookup?client=" + urllib.parse.quote(api_key, safe="")
        + "&duration=" + str(round(duration))
        + "&fingerprint=" + urllib.parse.quote(fp, safe="")
        + "&meta=recordings+releasegroups+compress&format=json"
    )
    last_err = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}: {e.read().decode(errors='replace')}"
        except Exception as e:
            last_err = str(e)
            time.sleep(2)
    return None, last_err


def best_match(resp):
    if not resp or resp.get("status") != "ok":
        return None
    results = sorted(resp.get("results", []), key=lambda r: -r.get("score", 0))
    if not results:
        return None
    top = results[0]
    score = top.get("score", 0)
    rec = next((r for r in top.get("recordings") or [] if r.get("title") and r.get("artists")), None)
    if rec is None:
        return {"score": score, "artist": None, "title": None, "album": None}
    artist = ", ".join(a["name"] for a in rec.get("artists", []))
    title = rec.get("title")
    rgs = rec.get("releasegroups") or []
    album = next((rg.get("title") for rg in rgs if rg.get("type") == "Album"), None)
    if album is None and rgs:
        album = rgs[0].get("title")
    return {"score": score, "artist": artist, "title": title, "album": album}


BRACKET_PAIRS = [("《", "》"), ("「", "」"), ("【", "】"), ("『", "』")]
LATIN_RUN = re.compile(r"[A-Za-z]+")  # 1+, not 2+: catches a lone "A"/"I" starting an English translation
LATIN_NAME = re.compile(r"^[A-Za-z .,'\-]+$")
CJK = re.compile(r"[一-鿿]")
YTID = re.compile(r"\s*\[[A-Za-z0-9_-]{11}\]\s*$")
TRAILING_PAREN_GROUP = re.compile(r"[\(（][^()（）]*[\)）]\s*$")
JUNK_SUFFIXES = [
    "Official Music Video", "Official MV", "Official Lyric Video", "Official Lyrics Video",
    "Lyrics Video", "Lyric Video", "官方歌詞版", "官方MV", "官方頻道", "歌詞版", "MV",
    "Lyrics", "歌詞", "歌词",
]
# Not a real artist name: a spoken preface segment, "Medley" (a multi-song
# mashup), a movie reference, or a bare year -- these only appear here because
# some dash/bracket split happened to land on them.
NOT_AN_ARTIST = re.compile(r"^(前言|Medley|\d{4}|.*(电影|電影|official|video|lyrics?|hd)\b)", re.IGNORECASE)


def cjk_only(s):
    """'周華健 Wakin Chau' -> '周華健'; '現在才說我愛你 Sorry...' -> '現在才說我愛你'.
    A string with no CJK at all (e.g. an English-only song title like
    "Morning call") is returned unchanged -- only truncate when there's CJK
    content followed by what looks like its English translation/name."""
    if not CJK.search(s):
        return s.strip(" ,&/⧸、")
    m = LATIN_RUN.search(s)
    return (s[:m.start()] if m else s).strip(" ,&/⧸、")


def strip_junk(title):
    changed = True
    while changed:
        changed = False
        t = title.strip()
        m = TRAILING_PAREN_GROUP.search(t)
        if m and not re.search(r"[《「『]", m.group()):
            t = t[: m.start()].strip()
            if t != title:
                title, changed = t, True
                continue
        for junk in JUNK_SUFFIXES:
            if t.lower().endswith(junk.lower()):
                t = t[: -len(junk)].strip(" -")
                changed = True
        if t != title:
            title, changed = t, True
    return title


QUALITY_TAG = re.compile(r"^(\d+[PK]|\d+\s*帧|\d+\s*fps|HD|4K|8K|高清|超清)\b", re.IGNORECASE)


def parse_brackets(base):
    """"<Artist(s)> -《<Title>》<junk>" or "<Artist(s)>《<Title>》<junk>"."""
    best = None
    for lb, rb in BRACKET_PAIRS:
        start = 0
        while True:
            li = base.find(lb, start)
            ri = base.find(rb, li + 1) if li != -1 else -1
            if li == -1 or ri == -1:
                break
            # Bilibili-style uploads often lead with a resolution/quality tag
            # bracket, e.g. "【1080P 60帧】Artist《Title》..." -- that's not
            # the title, so skip it and keep looking rightward for one that is.
            if QUALITY_TAG.match(base[li + 1:ri].strip()):
                start = ri + 1
                continue
            if best is None or li < best[0]:
                best = (li, ri, lb, rb)
            break
    if best is None:
        return None, None
    li, ri, _, _ = best
    # "<real title> (電影《<movie name>》...)" -- a bracket whose opener is
    # immediately preceded by "movie"/"电影" names a FILM, not the song; the
    # real title sits as plain prefix text we can't reliably isolate here.
    if re.search(r"(電影|电影|movie)\s*$", base[:li], re.IGNORECASE):
        return None, None
    title = cjk_only(base[li + 1:ri])
    # Drop any other bracket-tag content (e.g. a skipped quality tag) sitting
    # in the prefix before splitting it into artist name(s) -- otherwise its
    # partial text (cut off wherever cjk_only()'s Latin-run search lands)
    # pollutes the extracted artist.
    prefix = re.sub(r"[《「【『][^》」】』]*[》」】』]", "", base[:li])
    prefix = re.sub(r"\s*-\s*$", "", prefix).rstrip()
    parts = re.split(r"[&/⧸]", prefix)
    artists = [cjk_only(p) for p in parts if cjk_only(p)]
    # A real artist/band name here is almost always 2-5 CJK characters; a
    # longer unbroken run (no delimiter to split on) is far more likely to be
    # a name glued directly to promotional text, e.g. a CCTV clip titled
    # "谭咏麟经典咏唱！一首《定风波》..." ("Alan Tam classically sings! A...")
    # or "刘欢零点唱响《我欲成仙》" ("Liu Huan sang out at midnight...", where
    # 零点 means "midnight", not a second artist) -- naively taking the whole
    # prefix as the artist swallows that text too. Bail rather than guess.
    if any(len(a) > 5 for a in artists):
        return None, None
    artist = ", ".join(dict.fromkeys(artists))
    return artist, title


def parse_dash(base):
    """Plain "Artist - Title" (or reversed "Title - ArtistCJK - ArtistEnglish")."""
    if " - " not in base:
        return None, None
    segments = base.split(" - ")
    if len(segments) == 3 and LATIN_NAME.match(segments[2].strip()) \
            and CJK.search(segments[1]) and len(segments[1].strip()) <= 8:
        title, artist = segments[0].strip(), segments[1].strip()
    else:
        # cjk_only() strips a trailing English name/translation, e.g.
        # "王傑 Dave Wang" -> "王傑", so the artist matches the clean form
        # used elsewhere instead of carrying a redundant English suffix.
        artist, title = cjk_only(segments[0].strip()), " - ".join(segments[1:]).strip()
    return artist, strip_junk(title) if artist and title else (artist, title)


def parse_filename(path):
    base = os.path.splitext(os.path.basename(path))[0]
    base = YTID.sub("", base)
    for parser in (parse_brackets, parse_dash):
        artist, title = parser(base)
        if not artist or not title:
            continue
        title = strip_junk(title)
        if not title or "+" in title or len(artist) > 30 or NOT_AN_ARTIST.match(artist):
            continue
        return artist, title
    return None, None


def simplify(parts):
    """Traditional -> simplified Chinese via OpenCC, so results match the rest
    of the library (MusicBrainz's Cantopop data is often in traditional script)."""
    joined = SEP.join(p if p else "" for p in parts)
    try:
        r = subprocess.run(
            ["opencc", "-c", "t2s.json"],
            input=joined, capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.rstrip("\n").split(SEP)
        if len(out) == len(parts):
            return out
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return list(parts)


def write_tags(path, artist, title, album):
    f = MutagenFile(path, easy=True)
    if f is None:
        return False
    if f.tags is None:
        f.add_tags()
    f.tags["artist"] = artist
    f.tags["title"] = title
    if album:
        f.tags["album"] = album
    f.save()
    if path.lower().endswith(".mp3"):
        try:
            ID3(path).delete(path, delete_v1=True, delete_v2=False)
        except ID3NoHeaderError:
            pass
    return True


def process(path, api_key):
    return identify_and_tag(path, api_key)[0]


def identify_and_tag(path, api_key, hint=None, given=None):
    """Returns (summary line, artist, title); artist/title are None if the
    file couldn't be identified or tagged. `hint` is the source site's own
    {"artist", "title", "album"} metadata when it has any (YouTube Music
    tracks do), tried after AcoustID and before filename parsing. `given` is
    the same shape, typed by the user alongside the link: it overrides
    whatever identification finds, and if it has both artist and title,
    identification is skipped altogether."""
    base = os.path.basename(path)
    given = {k: v for k, v in (given or {}).items() if v}

    if given.get("artist") and given.get("title"):
        artist, title, album = given["artist"], given["title"], given.get("album")
        source = "given in message"
    else:
        artist, title, album, source, acoustid_note = auto_identify(path, api_key, hint)
        if given.get("artist"):
            if source is None:
                # Nothing identified the song, but with the artist known the
                # video title (minus the usual junk) is a fair title.
                title = parse_filename(path)[1] or title_without_artist(base, given["artist"])
                source = "artist given in message, title from video title"
            else:
                source = f"artist given in message, rest from {source}"
            artist = given["artist"]
        elif given.get("title") and source is not None:
            title = given["title"]
            source = f"title given in message, rest from {source}"
        if source is None:
            return f"? No match from AcoustID ({acoustid_note}) or filename; left {base} untagged", None, None
        album = given.get("album") or album

    artist, title, album = simplify([artist, title, album or ""])
    # MusicBrainz/uploaders often credit an English or stage name (e.g.
    # "G.E.M.", "Sandy Lam") -- map it to the name the library uses.
    artist = canonical_artist(artist)
    album = album or None
    if not write_tags(path, artist, title, album):
        return f"✗ Matched {artist} - {title} but could not write tags to {base}", None, None

    album_part = f", album {album}" if album else ""
    return f"✓ Tagged {base} as {artist} - {title}{album_part} ({source})", artist, title


def title_without_artist(base, artist):
    """Video title minus the [id], the artist's name (in any script or alias)
    and the usual junk: '鄧紫棋 泡沫 歌詞 [id].m4a' + '邓紫棋' -> '泡沫'."""
    text, simplified_artist = simplify([YTID.sub("", os.path.splitext(base)[0]), artist])
    artist = canonical_artist(simplified_artist)
    names = {simplified_artist, artist} | {
        alias for alias, name in ALIAS_NAMES.items() if name == artist
    }
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[#\s\-–—|｜:：]+", " ", text).strip()
    return strip_junk(text) or text


def auto_identify(path, api_key, hint):
    """AcoustID, then site metadata, then filename parsing. Returns
    (artist, title, album, source, acoustid_note); source is None if all
    three came up empty."""
    artist = title = album = source = None
    acoustid_note = "matched"

    if not api_key:
        acoustid_note = "ACOUSTID_API_KEY not set, skipped"
    else:
        fp_data, err = fingerprint(path)
        if err:
            acoustid_note = f"fingerprint failed: {err}"
        else:
            resp, err = lookup(api_key, fp_data["duration"], fp_data["fingerprint"])
            if err:
                acoustid_note = f"lookup failed: {err}"
            else:
                m = best_match(resp)
                if m and m.get("artist") and m["score"] >= CONFIDENCE_THRESHOLD:
                    artist, title, album = m["artist"], m["title"], m.get("album") or ""
                    source = f"AcoustID, score {m['score']:.2f}"
                else:
                    acoustid_note = f"no confident AcoustID match (score {m['score'] if m else 0:.2f})"

    if source is None and hint and hint.get("artist") and hint.get("title"):
        artist, title, album = hint["artist"], hint["title"], hint.get("album") or ""
        source = f"site metadata, {acoustid_note}"

    if source is None:
        artist, title = parse_filename(path)
        if artist and title:
            source = f"filename parse, {acoustid_note}"
    return artist, title, album, source, acoustid_note


def main():
    if len(sys.argv) < 2:
        print("usage: backfill_metadata.py FILE [FILE ...]", file=sys.stderr)
        sys.exit(1)
    api_key = os.environ.get("ACOUSTID_API_KEY", "").strip()
    for path in sys.argv[1:]:
        print(process(path, api_key))


if __name__ == "__main__":
    main()
