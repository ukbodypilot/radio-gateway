"""Verify the SOUNDBOARD_CATEGORIES filter."""
import os
import shutil
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from _tmpdirs import mkdtemp  # noqa: E402
import audio_sources  # noqa: E402

FP = audio_sources.FilePlaybackSource
FAIL = []



def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def src(value):
    s = object.__new__(FP)
    s.config = types.SimpleNamespace(SOUNDBOARD_CATEGORIES=value)
    return s


ALL = FP.soundboard_categories()
TOTAL = len(FP.SOUNDBOARD_POOL)

print(f"\n0. pool sanity ({TOTAL} sounds, {len(ALL)} categories)")
check("every pool entry is (str, int)",
      all(isinstance(c, str) and isinstance(i, int) for c, i in FP.SOUNDBOARD_POOL))
check("no duplicate (category, id) entries", len(set(FP.SOUNDBOARD_POOL)) == TOTAL)
check("counts sum to pool size", sum(ALL.values()) == TOTAL)
# Some ids are deliberately filed under several categories; picking must
# de-duplicate by id so one clip cannot occupy two slots.
_dupe_ids = TOTAL - len({i for _, i in FP.SOUNDBOARD_POOL})
print(f"       (note: {_dupe_ids} entries share an id with another category)")

print("\n1. blank filter = everything (back-compat)")
for val in ('', '   ', None):
    pool, note = src(val)._select_soundboard_pool()
    check(f"{val!r} -> full pool", len(pool) == TOTAL and note == '')

print("\n2. include list")
pool, note = src('boing, fart, scream')._select_soundboard_pool()
cats = {c for c, _ in pool}
check("only the named categories", cats == {'boing', 'fart', 'scream'}, str(cats))
check("size matches counts",
      len(pool) == ALL['boing'] + ALL['fart'] + ALL['scream'], f"{len(pool)}")
check("note names them", 'boing' in note and '3 categories' in note, note)

print("\n3. exclusions with '-'")
pool, _ = src('-animals, -applause')._select_soundboard_pool()
cats = {c for c, _ in pool}
check("excluded are gone", 'animals' not in cats and 'applause' not in cats)
check("everything else remains", len(cats) == len(ALL) - 2, f"{len(cats)}")
check("size matches", len(pool) == TOTAL - ALL['animals'] - ALL['applause'])

print("\n4. formatting tolerance")
pool_a, _ = src('BOING,  Fart ,scream')._select_soundboard_pool()
pool_b, _ = src('boing,fart,scream')._select_soundboard_pool()
check("case/space insensitive", sorted(pool_a) == sorted(pool_b))
pool_c, _ = src('boing,\nfart,,  ,scream')._select_soundboard_pool()
check("newlines and empty tokens ignored", sorted(pool_c) == sorted(pool_b))

print("\n5. unknown names are ignored, not fatal")
pool, _ = src('boing, banjo, nonsense')._select_soundboard_pool()
check("valid part still applies", {c for c, _ in pool} == {'boing'})
check("does not raise", True)

print("\n6. a filter that matches nothing falls back to the full pool")
pool, note = src('banjo, kazoo')._select_soundboard_pool()
check("falls back rather than going silent", len(pool) == TOTAL, f"{len(pool)}")
check("says why", 'matched nothing' in note, note)
pool, note = src('-' + ', -'.join(ALL))._select_soundboard_pool()
check("excluding everything also falls back", len(pool) == TOTAL, f"{len(pool)}")

print("\n7. include and exclude combined")
pool, _ = src('boing, fart, -fart')._select_soundboard_pool()
check("exclusion wins over inclusion", {c for c, _ in pool} == {'boing'})

print("\n8. config is read at call time (live reload, no restart)")
s = src('')
check("starts unfiltered", len(s._select_soundboard_pool()[0]) == TOTAL)
s.config.SOUNDBOARD_CATEGORIES = 'scream'
check("picks up the change immediately",
      {c for c, _ in s._select_soundboard_pool()[0]} == {'scream'})

print("\n9. missing attribute entirely (older config file)")
s = object.__new__(FP)
s.config = types.SimpleNamespace()
check("defaults to the full pool", len(s._select_soundboard_pool()[0]) == TOTAL)

print("\n10. picking never hands the same clip to two slots")
import random  # noqa: E402


def run_pick(cat_filter, slots=9, seed=0):
    """Drive the real _fill_soundboard_slots with downloads stubbed out."""
    s = object.__new__(FP)
    s.config = types.SimpleNamespace(SOUNDBOARD_CATEGORIES=cat_filter)
    s.announcement_directory = mkdtemp('soundboard-test')
    s.slot_count = 9      # these cases are written around 9 numbered slots
    s.file_status = {str(k): {'exists': False, 'path': '', 'filename': ''}
                     for k in range(10)}
    file_map = {}
    # Pretend every download succeeds instantly by pre-creating the cache file.
    cache = os.path.join(s.announcement_directory, '.cache')
    os.makedirs(cache, exist_ok=True)
    real_shuffle = random.shuffle

    def seeded(x):
        random.Random(seed).shuffle(x)
    random.shuffle = seeded
    try:
        # Pre-create files for the whole pool so no network access happens.
        for c, i in FP.SOUNDBOARD_POOL:
            open(os.path.join(cache, f"{c}_{i}.mp3"), 'wb').close()
        for k in list(range(slots + 1, 10)):
            file_map[str(k)] = ('local', f'local{k}.mp3')
        s._fill_soundboard_slots(file_map)
    finally:
        random.shuffle = real_shuffle
    return [v[1] for k, v in file_map.items() if v[0] != 'local']


for seed in range(40):
    names = run_pick('', seed=seed)
    ids = [n.rsplit('_', 1)[1] for n in names]
    if len(ids) != len(set(ids)):
        check(f"unique clips (seed {seed})", False, f"repeat in {sorted(names)}")
        break
else:
    check("40 seeds, unfiltered: no repeated clip", True)

# The worst case: a filter whose categories overlap on shared ids.
for seed in range(40):
    names = run_pick('boing, fart, funny', seed=seed)
    ids = [n.rsplit('_', 1)[1] for n in names]
    if len(ids) != len(set(ids)):
        check(f"unique clips, overlapping cats (seed {seed})", False,
              f"repeat in {sorted(names)}")
        break
else:
    check("40 seeds, boing/fart/funny (share ids 2890/2891/2894): no repeat", True)

names = run_pick('scream')          # 7 sounds, 9 slots
check("narrow filter fills what it can", 0 < len(names) <= 7, f"{len(names)} slots")

print("\n11. duration cap rejects long clips and remembers them")
import json  # noqa: E402


def cap_src(max_secs, durations, tmpdir=None):
    """Source whose downloads are stubbed; `durations` maps id -> seconds."""
    s = object.__new__(FP)
    s.config = types.SimpleNamespace(SOUNDBOARD_CATEGORIES='', SOUNDBOARD_MAX_SECONDS=max_secs)
    s.announcement_directory = tmpdir or mkdtemp('soundboard-test')
    s.slot_count = 9
    s.file_status = {str(k): {'exists': False, 'path': '', 'filename': ''} for k in range(10)}
    s._durations = durations
    s._sound_duration = lambda path: durations.get(
        int(os.path.basename(path).rsplit('_', 1)[1].split('.')[0]), 1.0)
    return s


def stub_downloads(s, seed=0):
    """Pre-create every cache file so no network access happens."""
    cache = os.path.join(s.announcement_directory, '.cache')
    os.makedirs(cache, exist_ok=True)
    for c, i in FP.SOUNDBOARD_POOL:
        open(os.path.join(cache, f"{c}_{i}.mp3"), 'wb').close()
    real = random.shuffle
    random.shuffle = lambda x: random.Random(seed).shuffle(x)
    return real


# Make every clip 60s except a handful, so the cap must hunt for short ones.
short_ids = {i for _, i in FP.SOUNDBOARD_POOL[:5]}
durs = {i: (2.0 if i in short_ids else 60.0) for _, i in FP.SOUNDBOARD_POOL}

s = cap_src(15, durs)
real = stub_downloads(s)
try:
    fm = {}
    s._fill_soundboard_slots(fm)
finally:
    random.shuffle = real
picked_ids = [int(v[1].rsplit('_', 1)[1].split('.')[0]) for v in fm.values()]
check("only clips under the cap are used",
      all(durs[i] <= 15 for i in picked_ids), str([(i, durs[i]) for i in picked_ids]))
check("fills from the short set", set(picked_ids) <= short_ids, str(picked_ids))
check("no over-long clip was assigned to a slot",
      not any(durs[i] > 15 for i in picked_ids))
check("rejected clips are not left in the cache",
      not os.path.exists(os.path.join(
          s.announcement_directory, '.cache',
          f"{FP.SOUNDBOARD_POOL[0][0]}_{FP.SOUNDBOARD_POOL[0][1]}.mp3"))
      or FP.SOUNDBOARD_POOL[0][1] in picked_ids)

print("\n12. the duration memo persists and is reused")
meta_path = s._soundboard_meta_path()
check("memo written outside .cache",
      os.path.exists(meta_path) and '.cache' not in meta_path, meta_path)
meta = json.load(open(meta_path))
check("memo recorded long clips", any(v > 15 for v in meta.values()), f"{len(meta)} entries")
# A refresh deletes .cache but must NOT delete the memo.
shutil.rmtree(os.path.join(s.announcement_directory, '.cache'))
check("memo survives a cache wipe", os.path.exists(meta_path))
s2 = cap_src(15, durs, tmpdir=s.announcement_directory)
check("memo is loaded back", len(s2._load_soundboard_meta()) == len(meta))

print("\n13. cap disabled (0) keeps everything")
s3 = cap_src(0, durs)
real = stub_downloads(s3)
try:
    fm3 = {}
    s3._fill_soundboard_slots(fm3)
finally:
    random.shuffle = real
check("all 9 slots filled regardless of length", len(fm3) == 9, f"{len(fm3)}")

print("\n14. _sound_duration is real and bounded")
tmp = mkdtemp('soundboard-test')
bogus = os.path.join(tmp, 'x_1.mp3')
open(bogus, 'wb').write(b'not an mp3')
check("undecodable file returns None", FP._sound_duration(bogus) is None)
check("missing file returns None", FP._sound_duration(os.path.join(tmp, 'nope_2.mp3')) is None)
_live = [f for f in __import__('glob').glob('audio/.cache/*.mp3')]
if _live:
    d0 = FP._sound_duration(_live[0])
    check(f"real clip measured ({os.path.basename(_live[0])})",
          isinstance(d0, float) and d0 > 0, f"{d0}")

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
