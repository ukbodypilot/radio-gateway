#!/usr/bin/env python3
"""Add, remove or list extra web-UI logins (WEB_EXTRA_USERS in gateway_config.txt).

    python3 tools/web_user.py add <name>      # asks for the password twice (or reads one line from stdin)
    python3 tools/web_user.py remove <name>
    python3 tools/web_user.py list

Only a salted PBKDF2 hash is written. The config line is edited in place (other lines,
comments and order are kept). Restart the gateway to apply.
"""
import getpass
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import web_users  # noqa: E402

CONFIG = os.environ.get('GATEWAY_CONFIG', os.path.join(ROOT, 'gateway_config.txt'))
_LINE = re.compile(r'^\s*WEB_EXTRA_USERS\s*=(.*)$')


def _read():
    with open(CONFIG) as f:
        lines = f.read().splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m:
            return lines, i, web_users.parse(m.group(1).split(' #', 1)[0])
    return lines, None, {}


def _write(lines, idx, users):
    new = f"WEB_EXTRA_USERS = {web_users.format_users(users)}\n"
    if idx is None:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append(new)
    else:
        lines[idx] = new
    tmp = CONFIG + '.tmp'
    with open(tmp, 'w') as f:
        f.writelines(lines)
    os.chmod(tmp, os.stat(CONFIG).st_mode & 0o777)
    os.replace(tmp, CONFIG)


def _password():
    if not sys.stdin.isatty():
        pw = sys.stdin.readline().rstrip('\n')
    else:
        pw = getpass.getpass('Password: ')
        if getpass.getpass('Again: ') != pw:
            sys.exit('Passwords differ, nothing changed.')
    if len(pw) < 8:
        sys.exit('Use at least 8 characters, nothing changed.')
    return pw


def main(argv):
    if len(argv) < 2 or argv[1] not in ('add', 'remove', 'list'):
        sys.exit(__doc__)
    lines, idx, users = _read()
    if argv[1] == 'list':
        print('\n'.join(users) or '(no extra users)')
        return
    if len(argv) != 3 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', argv[2]):
        sys.exit('Give one user name: letters, digits, _ . - (max 32).')
    name = argv[2]
    if argv[1] == 'add':
        existed = name in users
        users[name] = web_users.make_hash(_password())
        _write(lines, idx, users)
        print(f"{'Updated' if existed else 'Added'} {name}. Restart the gateway to apply.")
    else:
        if users.pop(name, None) is None:
            sys.exit(f'No extra user {name}.')
        _write(lines, idx, users)
        print(f"Removed {name}. Restart the gateway to apply.")


if __name__ == '__main__':
    main(sys.argv)
