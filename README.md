# telebot

Lightweight Telegram-to-shell bridge. Messages you send to the bot are
forwarded as the final argument to one configured command; the command's
output is sent back as the reply.

No inbound ports/webhooks required — it long-polls Telegram, so it works
fine behind NAT on a home NAS.

## How it works

- `bot.py` polls `getUpdates` in a loop.
- For each message from an allowed Telegram user ID, it runs:
  `$COMMAND <message text>` (no shell involved — the message arrives as one
  literal argument, so shell metacharacters in it are inert).
- Combined stdout+stderr (plus exit code if non-zero) is sent back as the
  reply, split into multiple messages if it exceeds Telegram's 4096-char
  limit.
- Messages older than `STARTUP_GRACE_PERIOD` seconds are ignored on startup,
  so a backlog that piled up while the bot was down doesn't get executed.

## Setup

1. **Create a bot**: message [@BotFather](https://t.me/BotFather) on
   Telegram, run `/newbot`, and copy the token it gives you.
2. **Get your user ID**: message [@userinfobot](https://t.me/userinfobot) and
   copy the numeric ID it replies with.
3. **Configure**:
   ```sh
   cp .env.example .env
   ```
   Edit `.env`: set `BOT_TOKEN`, `ALLOWED_USER_IDS` (comma-separated for
   multiple people), and `COMMAND`.
4. **Point `COMMAND` at your logic**. The default config uses
   `scripts/handle.sh`, mounted read-only into the container at `/scripts`.
   Edit `scripts/handle.sh` (or point `COMMAND` at your own script/binary)
   to do whatever you actually want triggered.
5. **Run it**. Either directly on the machine that has Docker:
   ```sh
   docker compose up -d --build
   ```
   or deployed to a remote NAS over SSH — see [Deploying to a NAS over
   SSH](#deploying-to-a-nas-over-ssh) below.
6. Message your bot on Telegram and confirm you get a reply.

## Downloading music (yt-dlp)

`scripts/handle.sh` treats any message starting with `http://` or `https://`
as a download link and hands it to `scripts/download.py`. `yt-dlp` and
`ffmpeg` are installed in the Docker image, along with `deno` (a JS runtime
yt-dlp needs for full YouTube support), so nothing extra needs to be
installed on the NAS itself.

- **Single videos or playlists.** A playlist link downloads every item (up to
  `MAX_PLAYLIST_ITEMS`, default 200); an unavailable item is reported and
  skipped rather than aborting the rest. Each song is tagged and filed as
  soon as it finishes, and the bot streams one line per song back to
  Telegram every `PROGRESS_INTERVAL` seconds instead of going silent until
  the whole playlist is done.
- **Telling it what the song is.** Anything after the link overrides
  automatic identification -- useful for uploads with messy titles:
  ```
  https://youtu.be/xxxx 邓紫棋 - 泡沫        artist - title
  https://youtu.be/xxxx 邓紫棋               artist only (title still auto-detected)
  https://youtu.be/xxxx
  artist: 邓紫棋
  title: 泡沫
  album: Xposed                             one key per line (歌手/歌名/专辑 work too)
  ```
  For a playlist link, artist/album apply to every song and a title is
  ignored.
- **Tagging.** `scripts/backfill_metadata.py` identifies each file, in order:
  AcoustID fingerprint lookup (real MusicBrainz artist/title/album), the
  site's own track metadata (YouTube Music uploads have it), then parsing
  the video title. Traditional Chinese is converted to simplified, and
  alternate artist names (`G.E.M.`, `Sandy Lam`, ...) are mapped to the
  library's name via `scripts/artist_aliases.json` -- add entries there as
  you find more. Needs `ACOUSTID_API_KEY` (free, from
  https://acoustid.org/api-key) for the fingerprint stage.
- **Filing.** The bot mounts the whole library at `/music`. New files land
  in `others/`, then move into the top-level folder named after the song's
  artist if one exists (matched ignoring case/spaces/punctuation, so
  `Taylor Swift` finds `TaylorSwift/`; for a multi-artist credit, the first
  artist with a folder wins). No new artist folders are created.
- **Dedup.** Two layers: yt-dlp's download archive
  (`/music/.telebot/download-archive.txt`) skips any video ID downloaded
  before -- seeded on every run with the `[<id>]` of every file already in
  the library -- and after tagging, a song whose artist, title and length
  (within ~6%) match an existing library file is deleted as a duplicate.
  The tag index this uses is cached in `/music/.telebot/index.json`; the
  first download reads every file's tags to build it, later ones only
  re-read files that changed.
- `COMMAND_TIMEOUT` is set to `1800` (30 minutes) in `.env`; raise it for
  big playlists -- whatever finished before a timeout is kept.
- yt-dlp needs occasional updates as sites change their pages. Bump it with:
  ```sh
  uv lock -P yt-dlp
  ./nas-deploy.sh
  ```
- The whole bot is single-threaded: while a download runs, it won't process
  any other Telegram messages until it finishes.

## Deploying to a NAS over SSH

`nas-deploy.sh` builds the image on your dev machine, ships it to the NAS
over SSH, and starts it there — no need to have this repo checked out or
`uv`/Docker Compose set up on the NAS itself, and no remote BuildKit (which
tends to time out over SSH on long builds).

```sh
./nas-deploy.sh --setup --host user@nas-hostname   # once, creates a Docker context named "nas"
./nas-deploy.sh                                     # sync scripts/, build, ship, (re)start
./nas-deploy.sh --logs                              # deploy and then tail logs
./nas-deploy.sh --down                              # stop and remove the container
```

Since `scripts/` is bind-mounted into the container, the NAS's own Docker
engine needs it on the NAS's filesystem, not just in this repo — the script
copies it over via `scp` on every deploy (Synology's `rsync` is wrapped by a
restricted DSM daemon that rejects plain SSH transport, so `scp` is used
instead). It also creates the music directory on the NAS if missing.

Defaults (override with flags — run `./nas-deploy.sh --help` for the full
list):
- `--scripts-dir /volume1/docker/telebot/scripts`
- `--music-dir /home/xdong/Music`
- `--platform linux/amd64` (pass `linux/arm64` for an ARM-based NAS)

`.env` and `docker-compose.yml` stay on your dev machine — `docker compose`
is invoked locally against the NAS's Docker context, so secrets are never
written to a file on the NAS itself (they're only held in the running
container's environment, same as any other Compose deployment).

## Security notes

- This service executes a command with attacker-controlled input (your own
  Telegram message) whenever the sender's user ID is on the allowlist. Only
  add user IDs you trust.
- Keep `BOT_TOKEN` and `.env` out of version control (already covered by
  `.gitignore`).
- The message is passed as a single argv element, not interpolated into a
  shell string, so it can't be used to chain extra shell commands unless
  your own `COMMAND` script itself passes it to a shell (e.g. `eval`,
  `sh -c "$1"`) — don't do that with untrusted senders.
- The `scripts/` directory is mounted read-only into the container for
  defense in depth.

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | Token from BotFather |
| `ALLOWED_USER_IDS` | yes | — | Comma-separated numeric Telegram user IDs |
| `COMMAND` | yes | — | Command to run; message text is appended as the last argument |
| `COMMAND_TIMEOUT` | no | `60` (set to `1800` in `.env`) | Seconds before the command is killed |
| `STARTUP_GRACE_PERIOD` | no | `60` | Ignore messages older than this many seconds at startup |
| `POLL_TIMEOUT` | no | `30` | Long-poll timeout passed to Telegram's `getUpdates` |
| `LOG_LEVEL` | no | `INFO` | Python logging level |
| `PROGRESS_INTERVAL` | no | `15` | Seconds between progress replies while a command is still running |
| `ACOUSTID_API_KEY` | no | — | Enables fingerprint-based tagging of downloads (see above); free key from acoustid.org/api-key |
| `MAX_PLAYLIST_ITEMS` | no | `200` | Cap on items downloaded from one playlist link |
| `DOWNLOAD_DIR` | no | `/music/others` | Where new downloads land (and stay, if no artist folder matches) |

## Running without Docker

Dependencies are managed with [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run --env-file .env bot.py
```

(`uv sync` reads `pyproject.toml`/`uv.lock` and creates `.venv` for you.
`uv run` does **not** load `.env` automatically — pass `--env-file .env`
explicitly, or `export UV_ENV_FILE=.env` once per shell so you can drop the
flag.)

Note: `.env`'s `COMMAND=/scripts/handle.sh` is the path *inside the
container* (where `docker-compose.yml` mounts `./scripts`). Running locally
without Docker, that absolute path won't exist on your machine — override it
for a quick local test, e.g.:
```sh
COMMAND=./scripts/handle.sh uv run --env-file .env bot.py
```

Wrap it in a systemd unit or your NAS's task scheduler to keep it running
across reboots.
