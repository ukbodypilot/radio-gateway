---
name: Gateway restart permission
description: Claude can restart the radio-gateway service during debugging sessions
type: feedback
---

Claude can restart the radio-gateway service (`sudo systemctl restart radio-gateway.service`) during active debugging sessions. The user granted this after start.sh was removed and restarts became simple Python process restarts.

**Why:** Rapid debug/restart cycles are needed during development. Having to ask the user to restart every time slows things down.

**How to apply:** During debugging, go ahead and restart the gateway when code changes need testing. Still avoid unnecessary restarts — only restart when changes require it.
