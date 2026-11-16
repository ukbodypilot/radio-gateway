#!/usr/bin/env python3
"""TX Log channel filter: one repeater at a time once several buses feed transcription.

A channel is bus + frequency tag. Every view on /txlog (history, keyword
search, activity charts) takes the same ?ch=bus|freq,... filter, so picking a
channel narrows the whole page, not just the table.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from transcription_log import TranscriptionLog   # noqa: E402

failed = 0


def check(name, ok, detail=''):
    global failed
    if not ok:
        failed += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {detail}" if detail and not ok else ''))


tmp = tempfile.mkdtemp()
tl = TranscriptionLog(types.SimpleNamespace(TRANSCRIPTION_LOG_PATH=os.path.join(tmp, 't.db'),
                                            TRANSCRIPTION_ALERT_KEYWORDS=''))
base = 1_800_000_000.0
rows = [('sdr1', '446.760', 'net control here, go ahead'),
        ('sdr2', '147.435', 'roger the fire is out'),
        ('sdr1', '446.760', 'fire on the ridge'),
        ('th9800', '145.300', 'morning all'),
        ('th9800', '146.520', 'simplex fire check'),
        ('', '', 'an old row from before buses')]
ids = []
for i, (bus, freq, text) in enumerate(rows):
    ids.append(tl.insert({'timestamp': base + i * 60, 'text': text, 'bus': bus, 'freq': freq,
                          'source': bus, 'duration': 2.0}))

print("\n1. insert returns the row id (speaker ID links overs to it)")
check("ids are returned and distinct", all(ids) and len(set(ids)) == len(ids), str(ids))

print("\n2. the channel list")
ch = tl.get_channels(30)
keys = [(c['bus'], c['freq']) for c in ch]
check("every bus+freq pair is its own channel (a retuned radio is two channels)",
      ('th9800', '145.300') in keys and ('th9800', '146.520') in keys and len(keys) == 5, str(keys))
check("busiest first", keys[0] == ('sdr1', '446.760'))

print("\n3. filtering")
P = TranscriptionLog.parse_channels
check("no filter -> everything", len(tl.get_recent(100, 0, None)) == 6)
one = tl.get_recent(100, 0, P('sdr1|446.760'))
check("one channel -> only its rows", [r['text'] for r in one] == ['fire on the ridge', 'net control here, go ahead'],
      str([r['text'] for r in one]))
two = tl.get_recent(100, 0, P('sdr1|446.760,sdr2|147.435'))
check("two channels combine", len(two) == 3)
check("bus alone does not match a different freq on that bus",
      len(tl.get_recent(100, 0, P('th9800|145.300'))) == 1)
check("rows with no bus are a channel too", len(tl.get_recent(100, 0, P('|'))) == 1)
s = tl.search_keyword('fire', 100, P('sdr1|446.760'))
check("keyword search honours the filter", [r['bus'] for r in s['results']] == ['sdr1'], str(s))
check("...and finds all channels without it", len(tl.search_keyword('fire', 100)['results']) == 3)
st = tl.get_hourly_stats(30, P('sdr2|147.435'))
check("activity stats honour the filter", sum(b[1] for b in st['buckets']) == 1 and st['total'] == 1, str(st))
check("empty / blank filter means all", P('') is None and P(None) is None and P(' , ') is None)
check("a hostile value is a parameter, never SQL",
      tl.get_recent(100, 0, P("x' OR 1=1 --|y")) == [])

print("\n4. the page uses the filter on every fetch")
html = open(os.path.join(ROOT, 'web_pages', 'txlog.html')).read()
fetches = re.findall(r"fetch\('(/transcri[^']*)'", html)
for f in fetches:
    # channels lists them; query is the "Ask" box, whose question names the
    # channel itself ("what was said on 446.76 today?") and becomes its own SQL.
    if f.startswith(('/transcription/channels', '/transcription/query')):
        continue
    line = html[html.index(f):].split('\n', 1)[0]
    check(f"fetch {f.split('?')[0]} carries chQuery()", 'chQuery()' in line, line.strip())
check("the selection is remembered per browser, inside try/catch",
      "localStorage.getItem('txlog.channels')" in html and 'try {' in html)
js = html.split('<script>', 1)[1].rsplit('</script>', 1)[0]
node = shutil.which('node')
if node:
    p = os.path.join(tmp, 'txlog.js')
    open(p, 'w').write(js)
    r = subprocess.run([node, '--check', p], capture_output=True, text=True)
    check("page script parses", r.returncode == 0, r.stderr[-300:])
else:
    print("  SKIP  node not installed; page script not syntax-checked")

tl.close()
print('FAILED' if failed else 'OK')
sys.exit(1 if failed else 0)
