# Speaker ID

Who is talking: every over the transcriber cuts is fingerprinted, matched to a
voice, and — as evidence builds up — tied to a person. It is a long-term game:
the first days are weak, every over and every label you give makes the next
day better, and nothing is ever thrown away, so better rules or models
re-score the whole history.

Off by default: `ENABLE_SPEAKER_ID = true` (needs `ENABLE_TRANSCRIPTION`).
It runs on every bus routed to the **transcription** sink (several buses may
feed it; each keeps its own stream) unless `SPEAKER_ID_BUSES` names a subset.

## How a voice gets a name

```
over (16 kHz, from the transcriber)
  -> fingerprint (ERes2Net, 192-d)             speaker_id/embedder.py, features.py
  -> live match against known voices           service.py   accept 0.65 / new < 0.45
  -> transcript arrives under the same id
  -> evidence filed on the over                identify.py
       callsign_self   "this is K0XYZ", call at the end, "... clear";
                       never a call in a question ("Who's that? K0XYZ?")
       name_cue        "thanks Jeff" (previous over), "go ahead Jeff" (next over)
       intro           "this is Quentin ... in Pine Hollow", "my name is Nora, I'm in Cedar Flat"
       mention         a call addressed or heard (activity only)
  -> verdict per voice from all its overs' evidence
  -> named when the bar is met                 >= 70% share and 2+ self-IDs on 2+ days,
                                                or 5+, or 3+ plus a matching FCC first name
  -> you confirm / correct on /speakers         always wins, never overridden
```

An introduction with a place is looked up in the FCC table: active
licensees with that first name (nicknames allowed) in that city, limited to
the states this channel's named voices hold licences in (there is an Oak
Park in California, Michigan and Illinois). Exactly one -> a "confirm" card
on a voice that never gave a call, or name support when it agrees with a
self-ID. It never names a voice by itself. Measured on 34k transcripts:
33 overs give a name and a city, 9 of them fit one licensee, and the 4 that
also spelled a call all agreed.

A call in a sentence ending "?" is asked about, not given, unless the
sentence opens with the cue ("This is K0XYZ, who's on frequency?"). Changing
the rules bumps `identify.VERSION`; the idle backfill then re-files every
stored over, keeping each call's measured position in the over.

Every callsign must be a real FCC licence (offline copy of the weekly public
ULS dump, `~/.config/radio-gateway/uls.db`, refreshed weekly in a child
process). That alone proves little — with ~820k active calls a garbled letter
run is often somebody's licence — so repetition, agreement with one voice, and
the regulars list (calls heard on many days; a rare near-miss folds onto an
established regular) are what make a name stick.

## The Speakers page (`/speakers`)

- **Review**: the questions where one click settles the most traffic —
  confirm a callsign, resolve a conflict, "same person?", "who is this?".
  Each card has sample clips to play.
- **Voices**: every voice, its label (you / auto), overs, channels, and what
  the evidence says. Click one for the "why we think so" list, the FCC
  licence, best samples and recent overs.
- Label, "not this person", merge two voices, move one over to another voice
  (or a voice of its own), mark two voices as different people.
- A call one letter off a voice's label (swapped, or dropped: K0XYZ heard
  as K0XY) raises no conflict card: that is the transcriber, not a second
  person.
- **Thin labels**: a label the voice has given as its own call fewer than
  twice is badged "1 self-ID" (filter: "thin labels only"). A conflict on a
  thin label ranks higher and says so: it is the likeliest to hide a second
  person.
- **Suggested split on a conflict card**: per other call, the overs where
  the voice gave it plus the overs that sound like them (same scoring as
  after a split), ticked with play buttons. If that call already has a
  voice the button moves them there; otherwise it splits them off as a new
  voice labelled with the call.
- **A voice that is two people** (a conflict card): tick the quoted overs
  that are someone else, "Split ticked into a new voice". The page then
  offers the voice's *other* overs that sound more like the new voice
  (score >= 0.65 against it and 0.05 above the rest of the old voice),
  ticked, with play buttons: untick any that are wrong, "Move ticked".
  "Find more" on a split in Recent changes asks again later, once the new
  voice has more overs. Always a suggestion; nothing moves without a click.
- **Recent changes**: every change, by you or by automation, with Undo.

The chip on `/transcribe` links to the voice.

## Two speakers in one over

On a busy net the next station often keys up within the transcriber's 1 s
hold, so one "over" holds two people. `SPEAKER_ID_SPLIT` (on) looks for
short pauses inside overs of 5 s or more and compares the voice either side.
A side comparison alone is too noisy on FM (it split 22% of ordinary long
overs), so a cut also needs the **veto** to pass: each whole side must match
a known voice (heard `SPLIT_VETO_MIN_N`+ times, score `SPLIT_VETO_AT`+) and
the two must be different people (two voices tied to one identity count as
one person). Each part becomes its own over with its own words and callsign;
the TX Log shows "A -> B".

Measured 2026-11-16: 0 of 76 same-person joins and 1 of 300 real long overs
split; about 21% of turnarounds caught once regulars are well heard. It
catches nothing until voices mature, which is the point: it never guesses.
Counters: `split_checked`, `split_overs`, `veto_known`, `veto_unknown`.

## Nightly

At `SPEAKER_ID_NIGHTLY_TIME` (default 03:30; "Re-cluster now" on the page):

1. recompute each voice from its confident overs (matched at >= 0.70);
2. move overs that now clearly fit another voice (>= 0.70 and 0.05 better),
   adopt "unsure" overs that now clear 0.70 — never out of a voice you
   labelled, never an over you placed;
   then **release** overs that no longer fit their own voice (below the live
   accept, 0.65) back to "unsure" — same exemptions, never a voice's first
   or last over; audited and undoable. This keeps an over placed under
   looser rules, or before a merge reshaped the voice, from staying for ever
   (first run on the rebuilt data: 9 of ~500);
3. merge voices >= `SPEAKER_ID_MERGE_AT` (0.80) alike — complete linkage,
   never across "different people" or two identities;
4. re-check names; prune clips older than `SPEAKER_ID_CLIP_DAYS` (30) except
   each voice's best 20 and anything you placed by hand.

Why 0.80: through one repeater and one processing chain the *average* voices
of two different people reached cosine 0.76 (24 h of SDR1, 2026-11-15);
merging at 0.60-0.70 joined strangers. Closer pairs go to the review queue.

## Model swap

Change `SPEAKER_ID_MODEL` (`eres2net`, `campplus`) and restart. The old model
stays live while every kept clip is re-fingerprinted with the new one in idle
time; then voices and labels switch over in one step. Overs whose clip was
pruned keep their old fingerprint and drop out of the new model's voices.

Bake-off (2026-11-15, 24 h of SDR audio, 40 self-identified overs): ERes2Net
6.3% EER, CAM++ zh+en 11.1%, VoxCeleb-only CAM++/ResNet exports 30-38%.

## Offload

`SPEAKER_ID_OFFLOAD = auto` fingerprints on a transcription worker that lists
the model in its `/status` `embed_models` — same code, so the same vector —
falling back to this box for any over the worker cannot take. Deploying the
files to a worker: see [transcription-pool.md](transcription-pool.md).

## Data

Private by nature; nothing is in the repo.

| Path | What |
|------|------|
| `~/.config/radio-gateway/speakers.db` | overs, fingerprints, voices, identities, evidence, audit |
| `~/.config/radio-gateway/speaker_clips/` | 12 kb/s Opus per over (~1.5 KB per second of speech) |
| `~/.config/radio-gateway/uls.db` | FCC amateur licences |

Backup (safe while running): `python3 -m speaker_id.retention backup out.tar.gz`.
`SPEAKER_ID_DB_PATH` moves the database; the clips live beside it.

### Rebuilding from the recordings

After a change to thresholds, splitting or evidence rules, the history can be
re-filed as the new code would have filed it live: `tools/speaker_rebuild.py`
replays the loop recordings (24 h kept) through a fresh speaker-ID service,
segmenting with the transcriber's own VAD settings and keeping only overs that
match a TX Log row (so each keeps its `tx_id` and start time). It writes a NEW
database; the live one is untouched. Human labels are not carried over.

It is heavy (about an hour of CPU per day of audio): run it on a
transcription worker. Copy this repo, `recordings/loop/<bus>/`, and sqlite
`.backup` snapshots of `transcriptions.db` and `uls.db` there, then:

```bash
python3 tools/speaker_rebuild.py --loop loop --tx-db transcriptions.db \
    --uls uls.db --out out --dry          # check the matching first
python3 tools/speaker_rebuild.py --loop loop --tx-db transcriptions.db --uls uls.db --out out
python3 tools/speaker_rebuild.py ... --topup    # later: add what was recorded since
```

Copy `out/` to `~/.config/radio-gateway/speakers-v2/`, set
`SPEAKER_ID_DB_PATH` to its `speakers.db`, restart. Rollback: blank the key.
First run (2026-11-16, 24 h of SDR1 + listener): self-IDs agreeing with
their voice 63% -> 90%, voices mixing callsigns 3 -> 2.

## Health

- `/speakers` status line: voices, named, queue, dropped, FCC table state,
  and the 7-day accuracy (below).
- **Accuracy trend** (`rg_speaker_id_quality{kind,window}`, 1d and 7d,
  refreshed every 10 min; Grafana "Speaker ID: accuracy"):
  `selfid_purity` = self-IDs ("this is CALL") sitting in the voice that most
  often gives CALL — the main signal, below 100% means voices still mix
  people; `selfid_agree` = self-IDs in named voices equal to the name;
  `placed_share` / `named_share` = overs put in a voice / a named voice;
  `selfids` = the sample size (under ~30 the line is noisy).
- `/transcriptions` → `speaker_id` (counters, offload, nightly, model switch).
- Prometheus: `rg_speaker_id_events_total{event}`, `rg_speaker_id_voices{kind}`;
  Grafana "Speaker ID" row.
- MCP: `speakers_list`, `speaker_whois`, `speaker_review`, `speaker_label`.
