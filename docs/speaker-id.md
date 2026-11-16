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
  -> live match against known voices           service.py   accept 0.60 / new < 0.45
  -> transcript arrives under the same id
  -> evidence filed on the over                identify.py
       callsign_self   "this is K0XYZ", call at the end, "... clear"
       name_cue        "thanks Jeff" (previous over), "go ahead Jeff" (next over)
       mention         a call addressed or heard (activity only)
  -> verdict per voice from all its overs' evidence
  -> named when the bar is met                 >= 70% share and 2+ self-IDs on 2+ days,
                                                or 5+, or 3+ plus a matching FCC first name
  -> you confirm / correct on /speakers         always wins, never overridden
```

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
- **Recent changes**: every change, by you or by automation, with Undo.

The chip on `/transcribe` links to the voice.

## Nightly

At `SPEAKER_ID_NIGHTLY_TIME` (default 03:30; "Re-cluster now" on the page):

1. recompute each voice from all its overs;
2. move overs that now clearly fit another voice (>= 0.70 and 0.05 better),
   adopt "unsure" overs that now clear 0.70 — never out of a voice you
   labelled, never an over you placed;
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

## Health

- `/speakers` status line: voices, named, queue, dropped, FCC table state.
- `/transcriptions` → `speaker_id` (counters, offload, nightly, model switch).
- Prometheus: `rg_speaker_id_events_total{event}`, `rg_speaker_id_voices{kind}`;
  Grafana "Speaker ID" row.
- MCP: `speakers_list`, `speaker_whois`, `speaker_review`, `speaker_label`.
