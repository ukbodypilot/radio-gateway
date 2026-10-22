#!/usr/bin/env python3
"""Mumble text commands: one handler per command, looked up in a table.

on_text_message used to be one 300-line if/elif. Pins that the table and
the !help text agree, that dispatch strips Mumble's HTML and is
case-insensitive, and that unknown commands get the help hint.
"""

import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import text_commands as tc  # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def gw_with(sent):
    return types.SimpleNamespace(
        config=types.SimpleNamespace(VERBOSE_LOGGING=False),
        mumble=types.SimpleNamespace(users={7: {'name': 'op'}}),
        send_text_message=sent.append)


def say(text):
    sent = []
    tc.on_text_message(gw_with(sent), types.SimpleNamespace(actor=7, message=text))
    return sent


def main():
    print("1. the table and !help agree")
    help_text = '\n'.join(say('!help'))
    listed = set(re.findall(r'^(![a-z]+)', help_text, re.M))
    table = set(tc._TEXT_COMMANDS)
    check("every listed command has a handler", listed <= table, str(sorted(listed - table)))
    check("every handler is listed (aliases aside)", table - listed <= {'!ep'}, str(sorted(table - listed)))
    check("!ep is an alias of !endpoints", tc._TEXT_COMMANDS['!ep'] is tc._TEXT_COMMANDS['!endpoints'])

    print("\n2. dispatch")
    calls = []
    real = dict(tc._TEXT_COMMANDS)
    tc._TEXT_COMMANDS['!loop'] = lambda gw, args: calls.append(args)
    try:
        say('<p>!LOOP  now &amp; then</p>')
    finally:
        tc._TEXT_COMMANDS.clear()
        tc._TEXT_COMMANDS.update(real)
    check("HTML stripped, entities decoded, case-insensitive", calls == ['now & then'], str(calls))
    check("unknown command gets the hint", say('!bananas') == ['Unknown command. Try !help'])
    check("plain chat is ignored", say('hello there') == [])

    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
