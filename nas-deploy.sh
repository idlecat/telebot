#!/usr/bin/env bash
set -euo pipefail

CONTEXT_NAME="nas"
COMPOSE_FILE="docker-compose.yml"

# ── helpers ──────────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --host USER@HOST        NAS SSH address (e.g. admin@192.168.1.100)
  --scripts-dir PATH      NAS-side path to hold scripts/, bind-mounted
                           read-only into the container at /scripts
                           (default: /volume1/docker/telebot/scripts)
  --music-dir PATH        NAS-side music library, bind-mounted at /music;
                           downloads land in its others/ folder and are
                           moved into matching artist folders
                           (default: /home/xdong/Music)
  --puid UID              UID the container runs as, so downloaded files are
                           owned by you instead of root (default: 1000)
  --pgid GID              GID the container runs as (default: 10)
  --platform PLATFORM     Image platform to build for (default: linux/amd64;
                           use linux/arm64 for an ARM-based NAS)
  --setup                 Create the Docker context (first run only)
  --logs                  Tail container logs after deploy
  --down                  Stop and remove the container
  --help                  Show this message
EOF
  exit 0
}

die() { echo "error: $1" >&2; exit 1; }

# ── parse args ────────────────────────────────────────────────────────────────
NAS_HOST=""
SCRIPTS_DIR_NAS="/volume1/docker/telebot/scripts"
MUSIC_DIR_NAS="/home/xdong/Music"
PUID="1000"
PGID="10"
PLATFORM="linux/amd64"
DO_SETUP=false
DO_LOGS=false
DO_DOWN=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --host)          NAS_HOST="$2";          shift 2 ;;
    --scripts-dir)   SCRIPTS_DIR_NAS="$2";   shift 2 ;;
    --music-dir)     MUSIC_DIR_NAS="$2";     shift 2 ;;
    --puid)          PUID="$2";              shift 2 ;;
    --pgid)          PGID="$2";              shift 2 ;;
    --platform)      PLATFORM="$2";          shift 2 ;;
    --setup)         DO_SETUP=true;          shift   ;;
    --logs)          DO_LOGS=true;           shift   ;;
    --down)          DO_DOWN=true;           shift   ;;
    --help)          usage ;;
    *)               die "unknown option: $1" ;;
  esac
done

# ── resolve repo root ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
cd "$REPO_ROOT"

# ── NAS command helpers ───────────────────────────────────────────────────────
# All NAS-targeted docker/compose calls go through these so the active context
# never matters — the script works regardless of what `docker context show` says.
# DOCKER_HOST env var takes precedence over --context, so unset it up front.
if [[ -n "${DOCKER_HOST:-}" ]]; then
  echo "note: unsetting DOCKER_HOST so --context $CONTEXT_NAME is used for NAS commands."
  unset DOCKER_HOST
fi

nas_docker()  { docker --context "$CONTEXT_NAME" "$@"; }
nas_compose() {
  SCRIPTS_DIR="$SCRIPTS_DIR_NAS" MUSIC_LIBRARY_DIR="$MUSIC_DIR_NAS" \
    PUID="$PUID" PGID="$PGID" \
    docker --context "$CONTEXT_NAME" compose -f "$COMPOSE_FILE" "$@"
}

# ── setup: create Docker context ──────────────────────────────────────────────
if $DO_SETUP; then
  [[ -n "$NAS_HOST" ]] || die "--host USER@HOST is required for --setup"
  if docker context inspect "$CONTEXT_NAME" &>/dev/null; then
    echo "Context '$CONTEXT_NAME' already exists — skipping creation."
  else
    docker context create "$CONTEXT_NAME" --docker "host=ssh://$NAS_HOST"
    echo "Context '$CONTEXT_NAME' created."
  fi
  echo "Run './nas-deploy.sh' (no --setup) to sync, build, and deploy."
  exit 0
fi

# ── verify context exists ─────────────────────────────────────────────────────
docker context inspect "$CONTEXT_NAME" &>/dev/null \
  || die "Docker context '$CONTEXT_NAME' not found. Run: $0 --setup --host USER@HOST"

# Resolve NAS endpoint from the named context (not from the active context)
NAS_HOST_URI="$(docker context inspect "$CONTEXT_NAME" \
  --format '{{.Endpoints.docker.Host}}' 2>/dev/null)"
NAS_IP="$(echo "$NAS_HOST_URI" | sed 's|ssh://||;s|/.*||;s|.*@||')"
# Full user@host for our own ssh/rsync calls (docker context handles its own
# ssh transport internally; rsync needs a separate connection).
NAS_SSH_HOST="$(echo "$NAS_HOST_URI" | sed 's|ssh://||;s|/.*||')"

# ── down ──────────────────────────────────────────────────────────────────────
if $DO_DOWN; then
  echo "Stopping telebot on NAS ($NAS_IP)..."
  nas_compose down
  echo "Done."
  exit 0
fi

# ── deploy ────────────────────────────────────────────────────────────────────
[[ -f "$REPO_ROOT/.env" ]] \
  || die ".env not found. Run: cp .env.example .env, then fill it in."

# scripts/ is bind-mounted into the container, which means the NAS's own
# Docker engine reads it straight off the NAS filesystem -- it has no access
# to this repo on your dev machine. Sync it over on every deploy so it's
# never stale, and make sure the music dir exists.
#
# scp, not rsync: Synology (and some other NAS) ship an rsync build wrapped
# by DSM's own restricted daemon that rejects ssh-transport paths outside
# its backup-app config, regardless of user permissions. scp has no such
# wrapper. We recreate SCRIPTS_DIR_NAS from scratch each deploy (equivalent
# to rsync --delete) since this directory only ever holds files this script
# manages. `-O` forces the legacy scp protocol -- the modern SFTP-based one
# errors on some NAS sshd builds when copying a directory recursively.
echo "Syncing scripts/ to $NAS_SSH_HOST:$SCRIPTS_DIR_NAS ..."
SCRIPTS_DIR_PARENT="$(dirname "$SCRIPTS_DIR_NAS")"
ssh "$NAS_SSH_HOST" "rm -rf '$SCRIPTS_DIR_NAS' && mkdir -p '$SCRIPTS_DIR_PARENT' '$MUSIC_DIR_NAS'"
scp -O -r "$REPO_ROOT/scripts" "$NAS_SSH_HOST:$SCRIPTS_DIR_PARENT/"
# scp copies the local "scripts" folder under that name; rename it if
# --scripts-dir asked for a different leaf name.
SCPD_REMOTE_NAME="$SCRIPTS_DIR_PARENT/scripts"
if [[ "$SCPD_REMOTE_NAME" != "$SCRIPTS_DIR_NAS" ]]; then
  ssh "$NAS_SSH_HOST" "mv '$SCPD_REMOTE_NAME' '$SCRIPTS_DIR_NAS'"
fi

# Building on the NAS over an SSH context causes BuildKit to open a gRPC
# tunnel back through the same SSH connection; the tunnel is closed after
# ~90 s of inactivity, which can kill a slow build mid-flight. Building
# locally avoids the remote BuildKit entirely and is usually faster too.
IMAGE_TAG="telebot:latest"
echo "Building image locally for $PLATFORM..."
docker --context default build \
  --platform "$PLATFORM" \
  -f "$REPO_ROOT/Dockerfile" \
  -t "$IMAGE_TAG" \
  "$REPO_ROOT"

echo "Transferring image to NAS ($NAS_IP)..."
docker --context default save "$IMAGE_TAG" \
  | nas_docker load

echo "Starting service on NAS ($NAS_IP)..."
nas_compose up -d --no-build --force-recreate

echo ""
echo "Deployed. Scripts: $SCRIPTS_DIR_NAS  Music: $MUSIC_DIR_NAS"
echo "Message your bot on Telegram to confirm it's responding."

if $DO_LOGS; then
  echo ""
  nas_compose logs -f
fi
