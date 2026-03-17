---
name: Automation Engine Status
description: Current state and next steps for the radio gateway automation engine feature
type: project
---

## Automation Engine — Implementation Status (2026-03-16)

**Committed and pushed to main** (commit dc49973).

### What's done
- `radio_automation.py` — all 5 classes: RepeaterDatabase, RadioController, AudioRecorder, SchemeParser, AutomationEngine
- `automation_scheme.txt` — example scheme file (all tasks commented out, engine starts with 0 tasks)
- `radio_gateway.py` changes — config, init, audio feed, web endpoints (/automationstatus, /automationhistory, /automationcmd), status bar, shutdown
- `start.sh` fix — was reading wrong config key `ENABLE_CAT_CONTROL` instead of `ENABLE_TH9800`, causing CAT service to stop on every restart
- RepeaterBook CSV loaded (115 repeaters, Orange County CA area): `RB_2603161801.csv`
- Tested: engine starts clean with 0 tasks, SDR + TH-9800 detected as available radios

### What's next — D75 full repeater programming
The D75 is the priority radio for automation because it supports full programmatic control via the `FO` (FrequencyInfo) CAT command — a single 21-field command that sets frequency, offset, shift direction, CTCSS/DCS tones, mode, all at once.

**Next step:** Implement `RadioController._tune_d75()` to use the `FO` command to program repeater parameters from the RepeaterDatabase, so the engine can autonomously tune to any repeater, set the correct TX offset and PL tone, and play announcements.

**Why:** The TH-9800 can only select pre-programmed memory channels via slow dial stepping (~0.5s per step). It cannot set frequency, tones, or offset via CAT. The D75 can do all of this programmatically.

**How to apply:** The user is switching to the Pi machine (where the D75 is connected) to continue this work. Pull the repo, enable automation in config, and implement the D75 FO command integration.

### D75 FO command details (from D75_CAT.py)
The `FO` command has 21 comma-separated fields:
0. Band (0 or 1)
1. Frequency (10-digit Hz, e.g., "0145500000")
2. Offset (10-digit Hz)
3. Step size
4. TX step size
5. Mode
6. Fine mode
7. Fine step size
8. Tone status (0/1)
9. CTCSS status (0/1)
10. DCS status (0/1)
11. CTCSS/DCS status (cross-tone, 0/1)
12. Reversed (0/1)
13. Shift direction (0=simplex, 1=up, 2=down)
14. Tone frequency index (0-38)
15. CTCSS frequency index (0-38)
16. DCS code index (0-103)
17. Cross-encode mode
18. URCALL
19. D-Star Squelch Type
20. D-Star Squelch Code

CTCSS_TONES array (38 tones): 67.0, 69.3, 71.9, ... 250.3
DCS_TONES array (104 codes): 023, 025, 026, ... 754

### Architecture
```
Text scheme file → AutomationEngine → Actions (tune, record, announce)
Future:  English mission → AI Planner → scheme → AutomationEngine → Actions
```

### Config keys (in gateway_config.txt)
```
ENABLE_AUTOMATION = true
AUTOMATION_SCHEME_FILE = automation_scheme.txt
AUTOMATION_REPEATER_FILE = RB_2603161801.csv
AUTOMATION_REPEATER_LAT = 0.0
AUTOMATION_REPEATER_LON = 0.0
AUTOMATION_RECORDINGS_DIR = recordings
AUTOMATION_START_TIME = 06:00
AUTOMATION_END_TIME = 23:00
AUTOMATION_MAX_TASK_DURATION = 600
```
