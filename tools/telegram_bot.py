#!/usr/bin/env python3
"""
Telegram Bot — Radio Gateway Control
======================================
Polls Telegram for messages from the authorized chat and injects them into
a running Claude Code tmux session.  Claude uses the gateway MCP tools to
handle requests and replies via the telegram_reply() MCP tool.

Voice/audio messages are handled directly (no Claude involvement):
downloaded, converted to PCM via ffmpeg, and streamed to the gateway's
announcement input port (9601) for immediate radio TX.

Architecture:
    Phone → Telegram → this bot → tmux send-keys → Claude Code (with MCP)
                                                         ↓
    Phone ← Telegram ← telegram_reply() MCP tool ← Claude response

    Phone → Telegram → this bot → ffmpeg → TCP port 9601 → radio TX (PTT auto)

Requirements:
    tmux   — running with a Claude Code session (see TELEGRAM_TMUX_SESSION)
    ffmpeg — for voice/audio message conversion
    No pip packages required — stdlib only.

Setup:
    1. Create a bot via @BotFather on Telegram → copy the token
    2. Send /start to the bot from your phone to get your chat ID
       (check the bot's getUpdates output or use @userinfobot)
    3. Set ENABLE_TELEGRAM = true in gateway_config.txt
    4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in gateway_config.txt
    5. Start Claude Code in a named tmux session:
           tmux new-session -s claude-gateway
           claude --dangerously-skip-permissions --model sonnet --effort medium
    6. Run this script (or enable the systemd service):
           python3 tools/telegram_bot.py

Systemd:
    sudo cp tools/telegram-bot.service /etc/systemd/system/
    sudo systemctl enable --now telegram-bot
"""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_KEYS = {
    'ENABLE_TELEGRAM':              False,
    'TELEGRAM_BOT_TOKEN':           '',
    'TELEGRAM_CHAT_ID':             0,
    'TELEGRAM_TMUX_SESSION':        'claude-gateway',
    'TELEGRAM_STATUS_FILE':         '/tmp/tg_status.json',
    'TELEGRAM_PROMPT_SUFFIX': (
        'When you have completely finished and are ready to respond, '
        'call telegram_reply() with your response. Do not call it until done.'
    ),
    # Seconds to wait for telegram_reply() after injecting a prompt before
    # warning the user. 0 disables the watchdog.
    'TELEGRAM_REPLY_TIMEOUT':       300,
    'ANNOUNCE_INPUT_HOST':          '127.0.0.1',
    'ANNOUNCE_INPUT_PORT':          9601,
    'AUDIO_SAMPLE_RATE':            48000,
    'AUDIO_CHUNK_SIZE':             2400,   # samples per chunk (50ms at 48kHz)
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
            # Empty string overrides are almost always a Save-wipe bug
            # (e.g. web Save dropping unknown keys to blank). Treat as
            # "unset" so the default still applies.
            if v == '':
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


def _download_file(token: str, file_id: str) -> bytes | None:
    """Download a Telegram file by file_id, return raw bytes or None."""
    info = _tg(token, 'getFile', {'file_id': file_id})
    if not info.get('ok'):
        return None
    file_path = info['result'].get('file_path', '')
    if not file_path:
        return None
    url = f'https://api.telegram.org/file/bot{token}/{file_path}'
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read()
    except Exception as e:
        print(f'[telegram] download error: {e}', flush=True)
        return None


# ---------------------------------------------------------------------------
# Audio announcement via port 9601
# ---------------------------------------------------------------------------

def _transmit_audio(pcm_bytes: bytes, host: str, port: int,
                    chunk_size: int, sample_rate: int) -> bool:
    """Send length-prefixed 16-bit mono PCM to the gateway announcement port.

    Sends at real-time rate (one chunk per tick interval) so the gateway's
    ANNIN queue (maxsize=16) never overflows and drops audio.
    """
    try:
        sock = socket.create_connection((host, port), timeout=5)
    except Exception as e:
        print(f'[telegram] cannot connect to announcement port {port}: {e}', flush=True)
        return False

    chunk_bytes = chunk_size * 2          # 16-bit = 2 bytes per sample
    tick_s     = chunk_size / sample_rate  # real-time interval per chunk (e.g. 0.05s)
    try:
        offset = 0
        next_send = time.monotonic()
        while offset < len(pcm_bytes):
            chunk = pcm_bytes[offset:offset + chunk_bytes]
            offset += chunk_bytes
            sock.sendall(struct.pack('>I', len(chunk)) + chunk)
            # Sleep until the next chunk is due so we don't flood the queue
            next_send += tick_s
            sleep_s = next_send - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)
    except Exception as e:
        print(f'[telegram] send error: {e}', flush=True)
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return True


def _handle_audio_message(token: str, chat_id: int, file_id: str,
                           label: str, cfg: dict) -> None:
    """Download, convert, and transmit a voice/audio message over radio."""
    print(f'[telegram] audio message ({label}) — downloading...', flush=True)
    raw = _download_file(token, file_id)
    if not raw:
        _send_message(token, chat_id, 'Failed to download audio file.')
        return

    sample_rate = int(cfg['AUDIO_SAMPLE_RATE'])
    chunk_size  = int(cfg['AUDIO_CHUNK_SIZE'])
    host        = cfg['ANNOUNCE_INPUT_HOST'] or '127.0.0.1'
    port        = int(cfg['ANNOUNCE_INPUT_PORT'])

    # Write to temp file so ffmpeg can detect format from header
    suffix = '.oga' if label == 'voice' else '.mp3'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_in:
        tmp_in.write(raw)
        tmp_in_path = tmp_in.name

    try:
        result = subprocess.run(
            [
                'ffmpeg', '-y', '-i', tmp_in_path,
                '-f', 's16le',          # raw signed 16-bit little-endian PCM
                '-acodec', 'pcm_s16le',
                '-ar', str(sample_rate),
                '-ac', '1',             # mono
                '-',
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            err = result.stderr.decode(errors='replace')[-300:]
            print(f'[telegram] ffmpeg error: {err}', flush=True)
            _send_message(token, chat_id, 'Audio conversion failed (ffmpeg error).')
            return

        pcm = result.stdout
        duration_s = len(pcm) / (sample_rate * 2)
        print(f'[telegram] converted {len(raw)} bytes → {len(pcm)} PCM bytes '
              f'({duration_s:.1f}s) — transmitting...', flush=True)

        ok = _transmit_audio(pcm, host, port, chunk_size, sample_rate)
        if ok:
            _send_message(token, chat_id,
                f'Transmitted {duration_s:.1f}s of audio over radio.')
        else:
            _send_message(token, chat_id,
                'Audio converted but could not reach gateway announcement port.\n'
                'Is the gateway running with `ENABLE_ANNOUNCE_INPUT = true`?')
    finally:
        os.unlink(tmp_in_path)


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
        # Clear anything already typed in the prompt box, so a half-typed
        # line (e.g. typed in the tmux session) can't get our prompt appended to it and
        # submitted as one garbled request. C-u is verified to clear real
        # input; when the box is empty it is a no-op, and it does NOT disturb
        # the greyed-out placeholder showing the previous prompt.
        subprocess.run(['tmux', 'send-keys', '-t', session, 'C-u'],
                       check=True, timeout=3)
        subprocess.run(['tmux', 'load-buffer', tmp], check=True, timeout=3)
        subprocess.run(['tmux', 'paste-buffer', '-t', session], check=True, timeout=3)
        # paste-buffer wraps the text in bracketed-paste escapes. Claude Code's
        # TUI needs a moment to leave paste-receiving mode before the Enter
        # gets interpreted as a submit (otherwise it lands inside the paste
        # and the prompt sits there waiting for ENTER).
        time.sleep(0.3)
        subprocess.run(['tmux', 'send-keys', '-t', session, 'Enter'], check=True, timeout=3)
        return True
    except Exception as e:
        print(f'[telegram] tmux inject error: {e}', flush=True)
        return False


def _read_status(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _capture_pane(session: str) -> str:
    try:
        r = subprocess.run(['tmux', 'capture-pane', '-p', '-t', session],
                           capture_output=True, text=True, timeout=5)
        return r.stdout
    except Exception:
        return ''


def _pane_answer(session: str) -> str:
    """Best-effort extraction of Claude's last answer from the pane.

    Claude Code prefixes assistant messages with a bullet, so take the last
    such block and stop at the input box / status line below it. Returns ''
    when nothing on screen looks like an answer.
    """
    lines = _capture_pane(session).splitlines()
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith('\u25cf'):
            start = i
            break
    if start is None:
        return ''
    out = []
    for ln in lines[start:]:
        t = ln.rstrip()
        stripped = t.lstrip()
        # Input box border, prompt line, or the thinking indicator = end.
        if stripped.startswith('\u2500') or stripped.startswith('\u276f') \
                or stripped.startswith('\u273b'):
            break
        out.append(t)
    return '\n'.join(out).strip().lstrip('\u25cf').strip()


def _watch_for_reply(token: str, chat_id: int, session: str, status_file: str,
                     baseline, question: str, timeout: int):
    """Warn the user if Claude never calls telegram_reply().

    That tool is the ONLY path back to the phone, so a missed call is
    invisible: "answered but never sent" and "never ran" both look like
    silence. telegram_reply() stamps last_reply_time in the status file, so
    poll it; if it hasn't moved by the deadline, send whatever is on screen
    rather than losing the answer entirely.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        if _read_status(status_file).get('last_reply_time') != baseline:
            return  # replied normally — stay quiet
    answer = _pane_answer(session)
    if answer:
        body = ('\u26a0\ufe0f Claude answered but never called telegram_reply(), so it '
                'was not sent. From the session:\n\n' + answer[:3000])
    else:
        body = (f'\u26a0\ufe0f No reply from Claude after {timeout}s for: '
                f'{question[:80]!r}\nThe session may be stuck or awaiting input.')
    _send_message(token, chat_id, body)
    print(f'[telegram] reply watchdog fired after {timeout}s '
          f'(answer_on_screen={bool(answer)})', flush=True)


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

                if from_id != chat_id:
                    print(f'[telegram] ignored message from unauthorized chat_id {from_id}', flush=True)
                    _send_message(token, from_id, 'Unauthorized. This bot is private.')
                    continue

                messages_today += 1
                ts = datetime.now().isoformat(timespec='seconds')

                # --- Voice note ---
                voice = msg.get('voice')
                if voice:
                    duration = voice.get('duration', 0)
                    print(f'[telegram] [{ts}] voice note ({duration}s)', flush=True)
                    _write_status(status_file, {
                        'last_message_time': ts,
                        'last_message_text': f'[voice {duration}s]',
                        'messages_today':    messages_today,
                    })
                    _handle_audio_message(token, chat_id, voice['file_id'], 'voice', cfg)
                    continue

                # --- Audio file ---
                audio = msg.get('audio')
                if audio:
                    title = audio.get('title') or audio.get('file_name', 'audio')
                    duration = audio.get('duration', 0)
                    print(f'[telegram] [{ts}] audio file: {title} ({duration}s)', flush=True)
                    _write_status(status_file, {
                        'last_message_time': ts,
                        'last_message_text': f'[audio: {title}]',
                        'messages_today':    messages_today,
                    })
                    _handle_audio_message(token, chat_id, audio['file_id'], 'audio', cfg)
                    continue

                # --- Text message → inject into Claude ---
                text = msg.get('text', '').strip()
                if not text:
                    continue

                print(f'[telegram] [{ts}] {text!r}', flush=True)

                # Snapshot BEFORE injecting, so the watchdog compares against
                # the reply that preceded this question.
                reply_baseline = _read_status(status_file).get('last_reply_time')
                tmux_ok = _inject(session, text, suffix)

                if tmux_ok and cfg['TELEGRAM_REPLY_TIMEOUT'] > 0:
                    threading.Thread(
                        target=_watch_for_reply,
                        args=(token, chat_id, session, status_file,
                              reply_baseline, text, cfg['TELEGRAM_REPLY_TIMEOUT']),
                        daemon=True).start()

                _write_status(status_file, {
                    'last_message_time': ts,
                    'last_message_text': text[:120],
                    'messages_today':    messages_today,
                    'tmux_active':       tmux_ok,
                })

                if not tmux_ok:
                    _send_message(token, chat_id,
                        f'⚠️ Claude tmux session `{session}` not found.\n'
                        f'Start it with:\n```\ntmux new-session -s {session}\nclaude --dangerously-skip-permissions --model sonnet --effort medium\n```'
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
