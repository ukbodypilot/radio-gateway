#!/usr/bin/env python3
"""
Telegram Bot — Radio Gateway Control
======================================
Polls Telegram for messages from the authorized chat and injects them into
a running Claude Code tmux session.  Claude uses the gateway MCP tools to
handle requests and replies via the telegram_reply() MCP tool.

Architecture:
    Phone → Telegram → this bot → tmux send-keys → Claude Code (with MCP)
                                                         ↓
    Phone ← Telegram ← telegram_reply() MCP tool ← Claude response

Requirements:
    tmux  — running with a Claude Code session (see TELEGRAM_TMUX_SESSION)
    No pip packages required — stdlib only.

Setup:
    1. Create a bot via @BotFather on Telegram → copy the token
    2. Send /start to the bot from your phone to get your chat ID
       (check the bot's getUpdates output or use @userinfobot)
    3. Set ENABLE_TELEGRAM = true in gateway_config.txt
    4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in gateway_config.txt
    5. Start Claude Code in a named tmux session:
           tmux new-session -s claude-gateway
           claude
    6. Run this script (or enable the systemd service):
           python3 tools/telegram_bot.py

Systemd:
    sudo cp tools/telegram-bot.service /etc/systemd/system/
    sudo systemctl enable --now telegram-bot
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_KEYS = {
    'ENABLE_TELEGRAM':        False,
    'TELEGRAM_BOT_TOKEN':     '',
    'TELEGRAM_CHAT_ID':       0,
    'TELEGRAM_TMUX_SESSION':  'claude-gateway',
    'TELEGRAM_STATUS_FILE':   '/tmp/tg_status.json',
    'TELEGRAM_PROMPT_SUFFIX': (
        'When you have completely finished and are ready to respond, '
        'call telegram_reply() with your response. Do not call it until done.'
    ),
}


def _load_config() -> dict:
    cfg = dict(_CONFIG_KEYS)
    cfg_path = Path(__file__).parent.parent / 'gateway_config.txt'
    if not cfg_path.is_file():
        return cfg
    with open(cfg_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip(); v = v.strip()
            if k not in cfg:
                continue
            default = cfg[k]
            if isinstance(default, bool):
                cfg[k] = v.lower() in ('true', '1', 'yes')
            elif isinstance(default, int):
                try:
                    cfg[k] = int(v)
                except ValueError:
                    pass
            else:
                cfg[k] = v
    return cfg


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def _tg(token: str, method: str, params: dict | None = None, timeout: int = 35) -> dict:
    url = f'https://api.telegram.org/bot{token}/{method}'
    data = json.dumps(params).encode() if params else None
    headers = {'Content-Type': 'application/json'} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        return {'ok': False, 'error': f'HTTP {e.code}: {body[:200]}'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _get_updates(token: str, offset: int, timeout: int = 30) -> list:
    result = _tg(token, 'getUpdates', {
        'offset': offset,
        'timeout': timeout,
        'allowed_updates': ['message'],
    }, timeout=timeout + 5)
    return result.get('result', []) if result.get('ok') else []


def _send_message(token: str, chat_id: int, text: str) -> bool:
    result = _tg(token, 'sendMessage', {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown',
    })
    return bool(result.get('ok'))


# ---------------------------------------------------------------------------
# Status file
# ---------------------------------------------------------------------------

def _write_status(path: str, updates: dict):
    existing = {}
    try:
        if os.path.isfile(path):
            with open(path) as f:
                existing = json.load(f)
    except Exception:
        pass
    existing.update(updates)
    try:
        with open(path, 'w') as f:
            json.dump(existing, f)
    except Exception as e:
        print(f'[telegram] status write error: {e}', flush=True)


# ---------------------------------------------------------------------------
# tmux injection
# ---------------------------------------------------------------------------

def _tmux_session_exists(session: str) -> bool:
    try:
        r = subprocess.run(
            ['tmux', 'has-session', '-t', session],
            capture_output=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def _inject(session: str, message: str, suffix: str) -> bool:
    if not _tmux_session_exists(session):
        print(f'[telegram] tmux session "{session}" not found', flush=True)
        return False
    full_prompt = f'[Telegram]: {message}'
    if suffix:
        full_prompt += f'\n{suffix}'
    # tmux send-keys requires literal string — use a temp file to avoid
    # shell escaping issues with special characters in user messages
    tmp = '/tmp/tg_prompt.txt'
    try:
        with open(tmp, 'w') as f:
            f.write(full_prompt)
        # Use tmux load-buffer then paste-buffer for reliable injection
        subprocess.run(['tmux', 'load-buffer', tmp], check=True, timeout=3)
        subprocess.run(['tmux', 'paste-buffer', '-t', session], check=True, timeout=3)
        subprocess.run(['tmux', 'send-keys', '-t', session, '', 'Enter'], check=True, timeout=3)
        return True
    except Exception as e:
        print(f'[telegram] tmux inject error: {e}', flush=True)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    cfg = _load_config()

    if not cfg['ENABLE_TELEGRAM']:
        print('[telegram] ENABLE_TELEGRAM = false — exiting', flush=True)
        sys.exit(0)

    token = cfg['TELEGRAM_BOT_TOKEN']
    chat_id = int(cfg['TELEGRAM_CHAT_ID'])
    session = cfg['TELEGRAM_TMUX_SESSION']
    status_file = cfg['TELEGRAM_STATUS_FILE']
    suffix = cfg['TELEGRAM_PROMPT_SUFFIX']

    if not token:
        print('[telegram] TELEGRAM_BOT_TOKEN not set — exiting', flush=True)
        sys.exit(1)
    if not chat_id:
        print('[telegram] TELEGRAM_CHAT_ID not set — exiting', flush=True)
        sys.exit(1)

    info = _tg(token, 'getMe')
    bot_username = info.get('result', {}).get('username', 'unknown') if info.get('ok') else 'unknown'
    print(f'[telegram] @{bot_username} | chat_id={chat_id} | tmux={session}', flush=True)

    _write_status(status_file, {
        'enabled':           True,
        'bot_running':       True,
        'bot_username':      f'@{bot_username}',
        'chat_id':           chat_id,
        'tmux_session':      session,
        'messages_today':    0,
        'last_message_time': None,
        'last_message_text': '',
        'last_reply_time':   None,
        'start_time':        datetime.now().isoformat(),
    })

    offset = 0
    messages_today = 0
    today = datetime.now().date()

    print('[telegram] Listening — waiting for messages...', flush=True)

    while True:
        try:
            # Reset daily counter at midnight
            if datetime.now().date() != today:
                today = datetime.now().date()
                messages_today = 0

            updates = _get_updates(token, offset)

            for upd in updates:
                offset = upd['update_id'] + 1
                msg = upd.get('message', {})
                from_id = msg.get('chat', {}).get('id', 0)
                text = msg.get('text', '').strip()

                if not text:
                    continue

                if from_id != chat_id:
                    print(f'[telegram] ignored message from unauthorized chat_id {from_id}', flush=True)
                    _send_message(token, from_id, 'Unauthorized. This bot is private.')
                    continue

                messages_today += 1
                ts = datetime.now().isoformat(timespec='seconds')
                print(f'[telegram] [{ts}] {text!r}', flush=True)

                tmux_ok = _inject(session, text, suffix)

                _write_status(status_file, {
                    'last_message_time': ts,
                    'last_message_text': text[:120],
                    'messages_today':    messages_today,
                    'tmux_active':       tmux_ok,
                })

                if not tmux_ok:
                    _send_message(token, chat_id,
                        f'⚠️ Claude tmux session `{session}` not found.\n'
                        f'Start it with:\n```\ntmux new-session -s {session}\nclaude\n```'
                    )

        except KeyboardInterrupt:
            print('\n[telegram] Stopped.', flush=True)
            _write_status(status_file, {'bot_running': False})
            break
        except Exception as e:
            print(f'[telegram] loop error: {e}', flush=True)
            time.sleep(5)


if __name__ == '__main__':
    run()
