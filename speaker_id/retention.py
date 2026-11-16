"""Clip retention and backups.

Clips are ~1.5 KB per second of speech (12 kb/s Opus); a busy repeater is a
few tens of MB a day. Embeddings and transcripts are kept forever (tiny);
clips older than `days` are deleted EXCEPT, per voice, the KEEP_BEST
best-matching ones (what a model swap re-fingerprints from and what the
Speakers page plays) and any over a human placed by hand.

Backup: `python -m speaker_id.retention backup <out.tar.gz>` writes a
consistent copy of speakers.db (SQLite online backup, safe while the gateway
runs) plus every kept clip. Restore = stop the gateway, unpack over
~/.config/radio-gateway/.
"""
import os
import sqlite3
import sys
import tarfile
import tempfile
import time

KEEP_BEST = 20


def prune(store, clip_dir, days, keep_best=KEEP_BEST):
    cutoff = time.time() - days * 86400
    with store._lock:
        old = store._conn.execute(
            "SELECT id, profile_id, clip FROM utterances WHERE clip <> '' AND ts < ?", (cutoff,)).fetchall()
        keep = {r[0] for r in store._conn.execute(
            "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY profile_id "
            "  ORDER BY score DESC, duration DESC) AS rk FROM utterances "
            "  WHERE clip <> '' AND profile_id IS NOT NULL) WHERE rk <= ?", (keep_best,))}
    rows = store.audit_rows(100_000)
    undone = {r['payload'].get('of') for r in rows if r['action'] == 'undo'}
    keep |= {r['payload']['utt_id'] for r in rows
             if r['action'] == 'move_over' and r['actor'] == 'human' and r['id'] not in undone}
    root = os.path.realpath(clip_dir)
    removed, freed = [], 0
    for uid, _pid, clip in old:
        if uid in keep:
            continue
        path = os.path.realpath(os.path.join(root, clip))
        if path.startswith(root + os.sep) and os.path.isfile(path):
            freed += os.path.getsize(path)
            os.unlink(path)
        removed.append(uid)
    if removed:
        with store._lock:
            store._conn.executemany("UPDATE utterances SET clip='' WHERE id=?", [(u,) for u in removed])
            store._conn.commit()
    for d in os.listdir(root) if os.path.isdir(root) else []:
        p = os.path.join(root, d)
        if os.path.isdir(p) and not os.listdir(p):
            os.rmdir(p)
    return {'clips_removed': len(removed), 'mb_freed': round(freed / 1e6, 1), 'clips_kept_old': len(old) - len(removed)}


def backup(db_path, clip_dir, out_path):
    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(out_path)) or '.') as tmp:
        snap = os.path.join(tmp, 'speakers.db')
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with tarfile.open(out_path, 'w:gz') as tar:
            tar.add(snap, arcname='speakers.db')
            if os.path.isdir(clip_dir):
                tar.add(clip_dir, arcname='speaker_clips')
    return out_path


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'backup':
        base = os.path.expanduser('~/.config/radio-gateway')
        print(backup(os.path.join(base, 'speakers.db'), os.path.join(base, 'speaker_clips'), sys.argv[2]))
    else:
        sys.exit(__doc__)
