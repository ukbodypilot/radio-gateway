"""Transcription keyword alerts are emailed, rate-limited, and never silent.

The old path POSTed to /telegram_send, a route that does not exist, inside a
bare `except: pass` -- so the keyword box on /transcribe silently did nothing.
check_keywords() now returns the hit and the transcriber emails it.
"""
import io
import os
import sys
import time
import types
import contextlib
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import transcriber
import transcription_log

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


TL = transcription_log.TranscriptionLog
tl = object.__new__(TL)
tl._config = types.SimpleNamespace(TRANSCRIPTION_ALERT_KEYWORDS='')
res = {'text': 'Mayday mayday this is a test', 'freq': '146.520', 'time_str': '12:00:00'}

print("\n1. check_keywords returns the hit instead of posting anywhere")
check("returns the matched keyword", tl.check_keywords(res, 'emergency, MAYDAY') == 'mayday')
check("case-insensitive", tl.check_keywords({'text': 'EMERGENCY now'}, 'emergency') == 'emergency')
check("no match returns None", tl.check_keywords(res, 'fire, flood') is None)
check("empty keyword list returns None", tl.check_keywords(res, '') is None)
check("no network call is made", 'urllib' not in TL.check_keywords.__code__.co_names)


class Notifier:
    def __init__(self, configured=True):
        self.configured, self.sent, self.done = configured, [], threading.Event()

    def is_configured(self):
        return self.configured

    def send(self, subject, body):
        self.sent.append((subject, body))
        self.done.set()
        return True


def make(notifier):
    t = object.__new__(transcriber.Transcriber) if hasattr(transcriber, 'Transcriber') else None
    if t is None:
        cls = [v for v in vars(transcriber).values()
               if isinstance(v, type) and hasattr(v, '_email_keyword_hit')][0]
        t = object.__new__(cls)
    t._gateway = types.SimpleNamespace(email_notifier=notifier)
    t._keyword_last_sent = {}
    return t


print("\n2. the hit is emailed, with the useful content")
n = Notifier()
t = make(n)
t._email_keyword_hit('mayday', res)
check("an email was sent", n.done.wait(2))
check("subject names keyword and frequency",
      "mayday" in n.sent[0][0] and "146.520" in n.sent[0][0], n.sent[0][0])
check("body carries the transcript", 'this is a test' in n.sent[0][1])

print("\n3. rate limit: one email per keyword per cooldown")
n = Notifier()
t = make(n)
t._email_keyword_hit('mayday', res)
n.done.wait(2)
t._email_keyword_hit('mayday', res)
time.sleep(0.2)
check("second hit inside the cooldown is suppressed", len(n.sent) == 1, str(len(n.sent)))
n.done.clear()
t._email_keyword_hit('fire', dict(res, text='fire on the hill'))
check("a different keyword still sends", n.done.wait(2) and len(n.sent) == 2)
t._keyword_last_sent['mayday'] -= t._KEYWORD_EMAIL_COOLDOWN + 1
n.done.clear()
t._email_keyword_hit('mayday', res)
check("sends again once the cooldown has passed", n.done.wait(2) and len(n.sent) == 3)

print("\n4. never silent when email is not configured")
buf = io.StringIO()
t = make(Notifier(configured=False))
with contextlib.redirect_stdout(buf):
    t._email_keyword_hit('mayday', res)
check("says so in the log", 'email is not configured' in buf.getvalue(), buf.getvalue().strip())
check("a failed attempt does not start the cooldown", 'mayday' not in t._keyword_last_sent)
t = make(None)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    t._email_keyword_hit('mayday', res)
check("no notifier at all is also logged", 'not configured' in buf.getvalue())

print("\n5. no hit is a no-op")
n = Notifier()
make(n)._email_keyword_hit(None, res)
time.sleep(0.1)
check("None keyword sends nothing", n.sent == [])

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
