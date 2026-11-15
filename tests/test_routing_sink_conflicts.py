"""One bus per sink — the rule, and the two API paths that enforce it.

Two buses wired to one sink never mix. Queue sinks interleave 50ms fragments
of each and then drop, the broadcastify L/R slots silently overwrite, and a
*_tx sink ends up with two _PttWorker threads on one radio: when the first
bus unkeys, the radio drops carrier and the second worker -- which only acts
on a state CHANGE -- never re-keys, so the second bus transmits into an
unkeyed radio with nothing logged.

Nothing checked for this before; the graph editor drew the second edge and
the config saved clean.
"""
import ast
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from routing_rules import (find_sink_conflicts, describe_conflicts,  # noqa: E402
                           strip_retired_sinks, RETIRED_SINKS)

FAIL = []
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def bs(bus, sink):
    return {'type': 'bus-sink', 'from': bus, 'to': sink}


print("\n1. the rule itself")
check("a lone bus per sink is legal",
      find_sink_conflicts([bs('a', 'mumble'), bs('b', 'speaker')]) == [])
c = find_sink_conflicts([bs('a', 'ic7100_tx'), bs('b', 'ic7100_tx')])
check("two buses on one sink is a conflict", len(c) == 1)
check("the conflict names the sink and both buses",
      c == [{'sink': 'ic7100_tx', 'busses': ['a', 'b']}], str(c))
check("the message says what to do instead",
      'one bus' in describe_conflicts(c) and 'ic7100_tx' in describe_conflicts(c),
      describe_conflicts(c))

print("\n2. what must stay legal")
check("a stale edge to the retired nul sink is never a conflict",
      find_sink_conflicts([bs('a', 'nul'), bs('b', 'nul'), bs('c', 'nul')]) == [])
check("the dual-channel feed is two buses into two SINKS",
      find_sink_conflicts([bs('sdr2', 'broadcastify_r'),
                           bs('allstar', 'broadcastify_l')]) == [])
check("the same edge saved twice is one edge, not a conflict",
      find_sink_conflicts([bs('a', 'speaker'), bs('a', 'speaker')]) == [])
check("source-bus edges are not sink edges",
      find_sink_conflicts([{'type': 'source-bus', 'from': 'a', 'to': 'b'},
                           {'type': 'source-bus', 'from': 'c', 'to': 'b'}]) == [])
check("empty / missing input is handled",
      find_sink_conflicts([]) == [] and find_sink_conflicts(None) == [])
check("malformed entries do not raise",
      find_sink_conflicts([{'type': 'bus-sink'}, {'type': 'bus-sink', 'to': 'x'}]) == [])

print("\n3. three buses on one sink report all three")
c = find_sink_conflicts([bs('a', 'mumble'), bs('b', 'mumble'), bs('c', 'mumble')])
check("all feeders listed", c == [{'sink': 'mumble', 'busses': ['a', 'b', 'c']}], str(c))

print("\n4. the live routing config is clean")
try:
    with open(os.path.join(ROOT, 'routing_config.json')) as f:
        live = json.load(f)
    live_c = find_sink_conflicts(live.get('connections', []))
    check("no conflicts in routing_config.json", live_c == [], str(live_c))
except FileNotFoundError:
    print("  SKIP  no routing_config.json here")

print("\n5. both write paths are actually gated")
# Source-level rather than live-call: the handlers are methods on the web
# server mixin and dragging the whole HTTP stack in to prove one early return
# would test the stack, not the rule.
with open(os.path.join(ROOT, 'web', 'routing_cmds.py')) as f:
    src = f.read()
tree = ast.parse(src)
fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
for fname in ('_routing_cmd_save_all', '_routing_cmd_connect'):
    body = ast.dump(fns[fname])
    check(f"{fname} calls find_sink_conflicts", 'find_sink_conflicts' in body)
    check(f"{fname} returns the refusal", 'describe_conflicts' in body)

# The gate must run BEFORE the write, or a conflict is persisted and only
# then reported -- which is how you get a saved config the gateway warns
# about on every boot.
save_src = ast.get_source_segment(src, fns['_routing_cmd_save_all'])
check("save_all refuses before _save_routing_config",
      save_src.index('find_sink_conflicts') < save_src.index('_save_routing_config'))
# Scope to the bus-sink branch: the source-bus branch above it has its own
# connections.append(conn) and would match first.
conn_src = ast.get_source_segment(src, fns['_routing_cmd_connect'])
sink_branch = conn_src[conn_src.index('elif bus and sink:'):]
check("connect refuses before appending the edge",
      sink_branch.index('find_sink_conflicts') < sink_branch.index('connections.append(conn)'))
check("the source-bus branch is left alone",
      'find_sink_conflicts' not in conn_src[:conn_src.index('elif bus and sink:')],
      "source->bus has no such constraint; a bus takes many sources")

print("\n6. BusManager warns but still builds the graph")
with open(os.path.join(ROOT, 'bus_manager.py')) as f:
    bm = f.read()
check("load path checks for conflicts", 'find_sink_conflicts(connections)' in bm)
check("it warns rather than raising or returning early",
      'WARNING: sink' in bm and 'raise' not in bm.split('find_sink_conflicts(connections)')[1][:400],
      "a routing mistake must not stop the gateway booting")

print("\n7. the retired 'nul' sink (2026-11-14): a bus needs no sink")
check("nul and recording are retired", {'nul', 'recording'} <= RETIRED_SINKS)
_cs = [bs('a', 'nul'), bs('b', 'speaker'), {'type': 'source-bus', 'from': 'x', 'to': 'nul'},
       bs('c', 'recording')]
check("strip_retired_sinks drops bus->nul / bus->recording edges only",
      strip_retired_sinks(_cs) == [bs('b', 'speaker'), {'type': 'source-bus', 'from': 'x', 'to': 'nul'}],
      str(strip_retired_sinks(_cs)))
check("strip_retired_sinks tolerates None", strip_retired_sinks(None) == [])
check("save_all strips retired sinks before the conflict check",
      save_src.index('strip_retired_sinks') < save_src.index('find_sink_conflicts'))
check("connect refuses a retired sink", 'RETIRED_SINKS' in sink_branch.split('connections.append(conn)')[0])
check("BusManager migration strips retired sinks on load",
      'strip_retired_sinks(connections)' in bm and 'RETIRED_SINKS' in bm)
with open(os.path.join(ROOT, 'web_server.py')) as f:
    check("the routing status no longer lists a 'nul' sink", "'id': 'nul'" not in f.read())
with open(os.path.join(ROOT, 'web_pages', 'routing.html')) as f:
    _html = f.read()
check("the routing page drops retired sinks an older gateway still lists",
      "RETIRED_SINKS = ['nul', 'recording']" in _html and 'RETIRED_SINKS.indexOf(s.id) < 0' in _html)
check("the routing page has no nul special cases left", 'isNul' not in _html and "!== 'nul'" not in _html)

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
