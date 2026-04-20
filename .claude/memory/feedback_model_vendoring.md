---
name: Vendoring ONNX / binary model files in-repo is preferred over runtime download
description: User is fine with ~16 MB binary blobs committed to the radio-gateway git repo if the alternative is a runtime download that can fail, hang, or cause first-use UX surprises. Don't default to lazy-download for model weights.
type: feedback
originSessionId: 0f1aa1d1-941b-4fe3-bef5-83a416b4760d
---
Session of 2026-04-19: I initially implemented DFN3 model loading as a lazy runtime download from GitHub releases because I was worried about git repo bloat. First-use hit a ~30 s blocking download that looked like a gateway crash to the user, who restarted the gateway to recover.

**User's reply:** "commit it, i don't care about 16mb lol, this is 2026"

**Why:** The UX cost of a runtime download (blocked feed worker, user confusion, network dependency, SHA-pin fragility) outweighs the repo-bloat cost for model weights under ~50 MB. Especially given this is a radio gateway deployed on offline-capable hardware where network reliability isn't guaranteed.

**How to apply:** When adding a new feature that needs a model weight file:

1. Default: commit it into `tools/models/<engine>/<filename>`.
2. Pin SHA256 in the code so corrupted bundled copies are detected.
3. Keep async-background-download as a *fallback* for forks that strip the binary — never as the primary path.
4. The bundled-first loader pattern already exists: `audio_util._dfn3_ensure_model()` checks bundled → cache → download, in that order.

Exception: models >50 MB or those with restrictive licenses probably still want git-LFS or runtime download, but ask first.
