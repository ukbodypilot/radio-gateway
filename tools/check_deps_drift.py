#!/usr/bin/env python3
"""Compare the Python packages requirements.txt declares against the ones
scripts/install.sh actually installs, and report drift.

Why this exists: install.sh does NOT read requirements.txt. It installs each
package explicitly, because several need special handling that a flat
`pip install -r` would get wrong — silero-vad is installed --no-deps (to
skip torch), pymumble has a fallback chain of
PyPI names, kv4p-ht-python is an editable clone of a git repo, and protobuf
has to be re-pinned last (see the pin-repair block in install.sh). So the two
lists are maintained by hand and can silently diverge.

They have diverged twice, both times the same way — a package the code
imports was missing from the installer, and the failure was invisible:
  * faster-whisper: every whisper/* model in the UI failed to load, including
    the starred recommendation.
  * prometheus-client: metrics.py's import is inside try/except at every call
    site, so instrumentation silently no-ops and /metrics returns 500 while
    install.sh cheerfully provisions Prometheus and a Grafana dashboard.

Exit status: 0 = no drift, 1 = drift found. Run it after touching either file.
"""

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ = os.path.join(REPO, 'requirements.txt')
INSTALLER = os.path.join(REPO, 'scripts', 'install.sh')

# Differences that are correct by design. Keep this list SHORT and justified —
# every entry is a place the two files are allowed to disagree.
EXPECTED_INSTALLER_ONLY = {
    # Bootstrap: needed before anything else can build. Not a runtime dep.
    'setuptools',
    # Re-pinned last by the pin-repair block to undo the upgrade that
    # onnxruntime forces. Declared by pymumble, not by us.
    'protobuf',
    # Fallback PyPI name for pymumble-py3; requirements.txt lists the primary.
    'pymumble',
}
EXPECTED_REQUIREMENTS_ONLY = {
    # Arrives transitively via onnx-asr[cpu].
    # Listed in requirements.txt so a manual install gets it explicitly.
    'onnxruntime',
}


def norm(name):
    """Normalise a distribution name for comparison (PEP 503-ish)."""
    return re.sub(r'[-_.]+', '-', name).strip().lower()


def parse_requirements(path):
    pkgs = set()
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Drop environment markers and version specifiers.
            line = line.split(';', 1)[0]
            m = re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*', line)
            if m:
                pkgs.add(norm(m.group(0)))
    return pkgs


def parse_installer(path):
    """Pull package names out of CORE_PKGS and every `_pip` invocation."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    pkgs = set()

    for m in re.finditer(r'^CORE_PKGS="([^"]*)"', text, re.M):
        for tok in m.group(1).split():
            pkgs.add(norm(tok))

    for m in re.finditer(r'_pip\s+([^\n|&]+)', text):
        for tok in m.group(1).split():
            tok = tok.strip().strip('\'"')
            if not tok or tok.startswith(('-', '$', '2>', '&')):
                continue          # flags, shell vars, redirections
            if tok in ('then', ';'):
                continue
            # Strip version specifiers/markers: protobuf==3.12.2 -> protobuf
            name = re.split(r'[=<>!~;\[]', tok)[0].strip()
            if name and re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*$', name):
                pkgs.add(norm(name))
    return pkgs


def main():
    for p in (REQ, INSTALLER):
        if not os.path.exists(p):
            print(f'ERROR: missing {p}', file=sys.stderr)
            return 2

    req = parse_requirements(REQ)
    inst = parse_installer(INSTALLER)

    missing_from_installer = req - inst - EXPECTED_REQUIREMENTS_ONLY
    missing_from_requirements = inst - req - EXPECTED_INSTALLER_ONLY

    print(f'requirements.txt declares {len(req)} packages; '
          f'install.sh installs {len(inst)}.')

    if not missing_from_installer and not missing_from_requirements:
        print('OK: no drift.')
        return 0

    if missing_from_installer:
        print('\nDECLARED but NOT INSTALLED by install.sh '
              '(a fresh install will lack these):')
        for p in sorted(missing_from_installer):
            print(f'  - {p}')
    if missing_from_requirements:
        print('\nINSTALLED but NOT DECLARED in requirements.txt '
              '(a manual pip install will lack these):')
        for p in sorted(missing_from_requirements):
            print(f'  - {p}')
    print('\nFix the lists, or add a justified entry to the EXPECTED_* sets '
          'in this script if the difference is deliberate.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
