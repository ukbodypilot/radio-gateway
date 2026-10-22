#!/usr/bin/env python3
"""config_schema is the one list of settings, and both readers agree with it.

Pins:
- every web-form key has a default, and every default is on the form or
  listed in NOT_IN_FORM with a reason (they had drifted by 40 keys)
- the example config sets only keys the schema knows
- keys inside a [plugin.instance] section no longer leak into the flat
  Config (a `MUMBLE_PORT` under [kv4p.vhf] used to override the real one)
- one bad value no longer aborts the load of every key after it
- the section reader uses the same comment rules as the flat loader
  ('/#' is a value, not a comment)
"""

import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _hwstubs  # noqa: E402,F401
import config_schema as cs  # noqa: E402
import endpoints_state  # noqa: E402
import radio_gateway  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


def write(text):
    fd, path = tempfile.mkstemp(prefix='rg_schema_', suffix='.txt')
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    return path


def main():
    print("1. one list of settings")
    form = cs.layout_keys()
    check("no key appears twice on the form", len(form) == len(set(form)),
          str(sorted(k for k in form if form.count(k) > 1)))
    check("every form key has a default", not set(form) - set(cs.DEFAULTS),
          str(sorted(set(form) - set(cs.DEFAULTS))))
    rest = set(cs.DEFAULTS) - set(form) - set(cs.NOT_IN_FORM)
    check("every default is on the form or in NOT_IN_FORM", not rest, str(sorted(rest)))
    check("NOT_IN_FORM keys are really off the form", not set(cs.NOT_IN_FORM) & set(form))
    check("NOT_IN_FORM names only real keys", not set(cs.NOT_IN_FORM) - set(cs.DEFAULTS))
    check("web form uses the schema's layout",
          radio_gateway.Config.__module__ and
          __import__('web_server').WebConfigServer._CONFIG_LAYOUT is cs.LAYOUT)

    print("\n2. the example config only sets known keys")
    unknown = []
    section = ''
    with open(os.path.join(REPO, 'examples', 'gateway_config.txt')) as f:
        for line in f:
            line = line.strip()
            if line.startswith('['):
                section = line
                continue
            m = re.match(r'([A-Z][A-Z0-9_]*)\s*=', line)
            if m and '.' not in section and m.group(1) not in cs.DEFAULTS and m.group(1) not in cs.RETIRED:
                unknown.append(m.group(1))
    check("no unknown keys", not unknown, str(unknown))
    check("retired keys really are unread", not set(cs.RETIRED) & set(cs.DEFAULTS))

    print("\n3. coerce and infer")
    check("bool", cs.coerce('ENABLE_GPS', 'yes') is True and cs.coerce('ENABLE_GPS', 'off') is False)
    check("hex int", cs.coerce('AIOC_VID', '0x1209') == 0x1209)
    check("decimal for an int key", cs.coerce('MUMBLE_PORT', '1.5') == 1.5)
    check("float key from an int string", cs.coerce('SIMPLEX_TAIL_TIME', '2') == 2.0
          and isinstance(cs.coerce('SIMPLEX_TAIL_TIME', '2'), float))
    check("unknown key inferred", (cs.infer('3'), cs.infer('3.5'), cs.infer('on'), cs.infer('x'))
          == (3, 3.5, True, 'x'))
    check("hex only where asked", cs.infer('0x10') == '0x10' and cs.infer('0x10', hex_ok=True) == 16)

    print("\n4. the flat loader")
    path = write("MUMBLE_PORT = 64739\n"
                 "GPS_BAUD = fast\n"           # bad value: warn, keep default
                 "VERBOSE_LOGGING = true\n"    # ...and keep loading after it
                 "PACKET_APRS_SYMBOL = /#\n"
                 "[kv4p.vhf]\n"
                 "port = /dev/ttyUSB0\n"
                 "MUMBLE_PORT = 1\n"
                 "[advanced]\n"
                 "LOG_FILE_DAYS = 3\n")
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        c = radio_gateway.Config(path)
    check("plugin-section key does not override the global one", c.MUMBLE_PORT == 64739, c.MUMBLE_PORT)
    check("plugin-section keys don't appear on Config", not hasattr(c, 'port'))
    check("a non-dotted section after it is read again", c.LOG_FILE_DAYS == 3, c.LOG_FILE_DAYS)
    check("bad value keeps its default", c.GPS_BAUD == 9600, c.GPS_BAUD)
    check("bad value is reported with its line", ':2: GPS_BAUD' in out.getvalue(), out.getvalue())
    check("keys after a bad value still load", c.VERBOSE_LOGGING is True)
    check("'/#' is a value", c.PACKET_APRS_SYMBOL == '/#', c.PACKET_APRS_SYMBOL)
    check("defaults for keys the file doesn't set", c.SIMPLEX_MAX_BUFFER == 30.0
          and c.GPS_PORT == '/dev/ttyACM0')
    os.unlink(path)

    print("\n5. the section reader")
    path = write("[kv4p.vhf]\n"
                 "port = /dev/ttyUSB0   # the VHF one\n"
                 "ctcss = 0x10\n"
                 "enable = yes\n"
                 "note = \"a # b\"\n"
                 "tag = /#\n"
                 "[kv4p.uhf]\n"
                 "default_freq = 446.0\n")
    s = endpoints_state.read_sections('kv4p', path)
    check("instances found", sorted(s) == ['uhf', 'vhf'], str(s))
    v = s.get('vhf', {})
    check("inline comment stripped", v.get('port') == '/dev/ttyUSB0', v.get('port'))
    check("hex, bool", v.get('ctcss') == 16 and v.get('enable') is True)
    check("quoted '#' kept", v.get('note') == 'a # b', v.get('note'))
    check("'/#' kept (the old reader cut it to '/')", v.get('tag') == '/#', v.get('tag'))
    check("float", s.get('uhf', {}).get('default_freq') == 446.0)
    os.unlink(path)

    print("\nALL PASS" if not failed else f"\n{failed} FAILED")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
