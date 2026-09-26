#!/usr/bin/env python3
"""Telegram -> shell command bridge.

Polls Telegram for messages from an allowlisted set of user IDs, runs a
single configured command with the message text as its final argument, and
replies with the command's combined stdout/stderr output.
"""

import logging
import os
import shlex
import subprocess
import sys
import time

import requests

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("telebot")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

TELEGRAM_MAX_MESSAGE_LEN = 4096
REPLY_CHUNK_LEN = 4000  # leave headroom for truncation notices


class Config:
    def __init__(self):
        self.bot_token = self._require("BOT_TOKEN")
        self.allowed_user_ids = self._parse_ids(self._require("ALLOWED_USER_IDS"))
        self.command = self._require("COMMAND")
        self.command_argv = shlex.split(self.command)
        self.command_timeout = float(os.environ.get("COMMAND_TIMEOUT", "60"))
        self.startup_grace_period = float(os.environ.get("STARTUP_GRACE_PERIOD", "60"))
        self.poll_timeout = int(os.environ.get("POLL_TIMEOUT", "30"))

    @staticmethod
    def _require(name):
        value = os.environ.get(name)
        if not value:
            log.error("Missing required environment variable: %s", name)
            sys.exit(1)
        return value

    @staticmethod
    def _parse_ids(raw):
        ids = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                log.error("Invalid entry in ALLOWED_USER_IDS: %r", part)
                sys.exit(1)
        if not ids:
            log.error("ALLOWED_USER_IDS must contain at least one numeric user ID")
            sys.exit(1)
        return ids


def api_call(config, method, params=None, timeout=10):
    url = TELEGRAM_API.format(token=config.bot_token, method=method)
    resp = requests.get(url, params=params or {}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data}")
    return data["result"]


def send_message(config, chat_id, text):
    if not text:
        text = "(command produced no output)"
    for start in range(0, len(text), REPLY_CHUNK_LEN):
        chunk = text[start : start + REPLY_CHUNK_LEN]
        try:
            api_call(
                config,
                "sendMessage",
                params={"chat_id": chat_id, "text": chunk},
            )
        except Exception:
            log.exception("Failed to send message to chat %s", chat_id)


def run_command(config, message_text):
    argv = config.command_argv + [message_text]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=config.command_timeout,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {config.command_timeout}s"
    except OSError as exc:
        return f"Failed to run command: {exc}"

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        output += f"\n[exit code {result.returncode}]"
    return output.strip()


def handle_update(config, update, startup_time):
    message = update.get("message")
    if not message or "text" not in message:
        return

    from_user = message.get("from", {})
    user_id = from_user.get("id")
    chat_id = message["chat"]["id"]
    text = message["text"]
    msg_date = message.get("date", 0)

    if msg_date < startup_time - config.startup_grace_period:
        log.info("Ignoring stale message from user %s (dated %s)", user_id, msg_date)
        return

    if user_id not in config.allowed_user_ids:
        log.warning("Ignoring message from unauthorized user %s", user_id)
        return

    log.info("Running command for user %s: %r", user_id, text)
    output = run_command(config, text)
    send_message(config, chat_id, output)


def main():
    config = Config()
    startup_time = time.time()
    log.info(
        "Starting bot. Allowed users: %s. Command: %r",
        sorted(config.allowed_user_ids),
        config.command,
    )

    offset = None
    while True:
        try:
            updates = api_call(
                config,
                "getUpdates",
                params={
                    "timeout": config.poll_timeout,
                    "offset": offset,
                    "allowed_updates": '["message"]',
                },
                timeout=config.poll_timeout + 10,
            )
        except requests.exceptions.RequestException:
            log.exception("Network error while polling Telegram; retrying in 5s")
            time.sleep(5)
            continue
        except Exception:
            log.exception("Unexpected error while polling Telegram; retrying in 5s")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(config, update, startup_time)
            except Exception:
                log.exception("Error handling update %s", update.get("update_id"))


if __name__ == "__main__":
    main()
