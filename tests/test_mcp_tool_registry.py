"""The MCP tool registry matches the code, the docs, and the server's routes.

Guards three drifts found in the 2026-09-23 audit:
  * a removed tool (broadcastify_control) still listed in docs/mcp.md and the
    README count;
  * a shadowed tool -- two @mcp.tool functions with one name silently register
    once, so the advertised count is wrong;
  * new server features (BGM, announcer, TTS engine, soundboard) shipping with
    no tool at all.
"""
import ast
import asyncio
import glob
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


import mcp_server
mcp_server._register_all_tools()
from mcp_server.server import mcp

registered = {t.name for t in asyncio.run(mcp.list_tools())}

decorated = []
for f in glob.glob(os.path.join(ROOT, 'mcp_server', 'tools', '*.py')):
    for n in ast.walk(ast.parse(open(f).read())):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                'mcp.tool' in ast.unparse(d) for d in n.decorator_list):
            decorated.append(n.name)

check("no two tools share a name (a duplicate silently shadows)",
      len(decorated) == len(set(decorated)),
      str(sorted({n for n in decorated if decorated.count(n) > 1})))
check("every decorated function is registered", set(decorated) == registered,
      str(sorted(set(decorated) ^ registered)))

doc = open(os.path.join(ROOT, 'docs', 'mcp.md')).read()
documented = set(re.findall(r'`([a-z][a-z0-9_]+)`', doc))
check("every registered tool is listed in docs/mcp.md",
      not (registered - documented), str(sorted(registered - documented)))
check("docs/mcp.md lists no tool that was removed",
      'broadcastify_control' not in registered and 'broadcastify_control' not in documented)

n = len(registered)
for rel, pat in (('README.md', r'(\d+) MCP tools'), ('docs/index.md', r'(\d+) MCP tools'),
                 ('docs/mcp.md', r'\*\*(\d+) tools\*\*')):
    m = re.search(pat, open(os.path.join(ROOT, rel)).read())
    check(f"{rel} advertises the real count ({n})", bool(m) and int(m.group(1)) == n,
          m.group(0) if m else 'no count found')

for t in ('bgm_control', 'announcer_configure', 'tts_engine_set', 'soundboard_set_categories'):
    check(f"{t} is registered", t in registered)

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
