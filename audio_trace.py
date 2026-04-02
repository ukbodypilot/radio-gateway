"""Audio pipeline debug trace — watchdog and HTML trace dump.

Extracted from gateway_core.py. These are diagnostic tools, not part
of the normal audio path.
"""

import time
import os
import statistics
import datetime
import resource
import platform


def watchdog_trace_loop(gw):
    """Low-fidelity long-running trace.  Samples every 5s, flushes to disk every 60s.
    Designed to run overnight to diagnose freezes."""
    from gateway_core import __version__
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'watchdog_trace.txt')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    SAMPLE_INTERVAL = 5     # seconds between samples
    FLUSH_INTERVAL = 60     # seconds between disk writes
    buffer = []
    last_flush = time.monotonic()
    prev_tick = gw._tx_loop_tick

    # Write/append header
    hdr = ("timestamp\tuptime_s\ttx_ticks\ttick_rate"
           "\tth_tx\tth_stat\tth_kb\tth_aioc\tth_sdr1\tth_sdr2\tth_remote\tth_announce"
           "\tmumble"
           "\ten_aioc\ten_sdr1\ten_sdr2\ten_remote\ten_announce"
           "\tmu_tx\tmu_rx\tmu_sdr1\tmu_sdr2\tmu_remote\tmu_announce\tmu_spk"
           "\tlvl_tx\tlvl_rx\tlvl_sdr1\tlvl_sdr2\tlvl_sv"
           "\tq_aioc\tq_sdr1\tq_sdr2"
           "\tptt\tvad\trebro_ptt\trss_mb\n")
    try:
        with open(out_path, 'a') as f:
            f.write(f"\n# Watchdog started {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    f"  v{__version__}"
                    f"  {platform.node()} {platform.system()} {platform.release()} {platform.machine()}"
                    f"  py{platform.python_version()}\n")
            f.write(hdr)
    except Exception:
        pass

    while gw._watchdog_active and gw.running:
        time.sleep(SAMPLE_INTERVAL)
        if not gw._watchdog_active:
            break

        now_mono = time.monotonic()
        uptime = now_mono - gw._watchdog_t0

        # Tick rate (ticks per second since last sample)
        cur_tick = gw._tx_loop_tick
        tick_rate = (cur_tick - prev_tick) / SAMPLE_INTERVAL
        prev_tick = cur_tick

        # Thread alive checks
        def _alive(t):
            return 1 if (t and t.is_alive()) else 0

        th_tx = _alive(gw._tx_thread)
        th_stat = _alive(gw._status_thread)
        th_kb = _alive(gw._keyboard_thread)
        th_aioc = _alive(gw.radio_source._rx_thread if gw.radio_source and hasattr(gw.radio_source, '_rx_thread') else None)
        th_sdr1 = _alive(None)
        th_sdr2 = _alive(None)
        th_remote = _alive(gw.remote_audio_source._reader_thread if gw.remote_audio_source and hasattr(gw.remote_audio_source, '_reader_thread') else None)
        th_announce = _alive(gw.announce_input_source._reader_thread if gw.announce_input_source and hasattr(gw.announce_input_source, '_reader_thread') else None)

        # Mumble connection
        mumble_ok = 0
        try:
            if gw.mumble and gw.mumble.is_alive():
                mumble_ok = 1
        except Exception:
            pass

        # Source enabled flags
        en_aioc = 1 if (gw.radio_source and gw.radio_source.enabled) else 0
        en_sdr1 = 1 if (gw.sdr_plugin and gw.sdr_plugin.tuner1_enabled) else 0
        en_sdr2 = 1 if (gw.sdr_plugin and gw.sdr_plugin.tuner2_enabled) else 0
        en_remote = 1 if (gw.remote_audio_source and gw.remote_audio_source.enabled) else 0
        en_announce = 1 if (gw.announce_input_source and gw.announce_input_source.enabled) else 0

        # Mute flags
        mu_tx = 1 if gw.tx_muted else 0
        mu_rx = 1 if gw.rx_muted else 0
        mu_sdr1 = 1 if gw.sdr_muted else 0
        mu_sdr2 = 1 if gw.sdr2_muted else 0
        mu_remote = 1 if gw.remote_audio_muted else 0
        mu_announce = 1 if gw.announce_input_muted else 0
        mu_spk = 1 if gw.speaker_muted else 0

        # Audio levels
        lvl_tx = gw.tx_audio_level
        lvl_rx = gw.rx_audio_level
        lvl_sdr1 = gw.sdr_audio_level
        lvl_sdr2 = gw.sdr2_audio_level
        lvl_sv = gw.sv_audio_level

        # Queue depths
        def _qsize(src):
            try:
                for attr in ('_rx_queue', '_chunk_queue'):
                    q = getattr(src, attr, None)
                    if q is not None:
                        return q.qsize()
            except Exception:
                pass
            return -1

        q_aioc = _qsize(gw.radio_source)
        q_sdr1 = 0
        q_sdr2 = 0

        # PTT / VAD / rebroadcast
        ptt = 1 if gw.ptt_active else 0
        vad = 1 if gw.vad_active else 0
        rebro = 1 if gw._rebroadcast_ptt_active else 0

        # RSS memory (KB → MB)
        try:
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            rss_mb = -1

        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        line = (f"{ts}\t{uptime:.0f}\t{cur_tick}\t{tick_rate:.1f}"
                f"\t{th_tx}\t{th_stat}\t{th_kb}\t{th_aioc}\t{th_sdr1}\t{th_sdr2}\t{th_remote}\t{th_announce}"
                f"\t{mumble_ok}"
                f"\t{en_aioc}\t{en_sdr1}\t{en_sdr2}\t{en_remote}\t{en_announce}"
                f"\t{mu_tx}\t{mu_rx}\t{mu_sdr1}\t{mu_sdr2}\t{mu_remote}\t{mu_announce}\t{mu_spk}"
                f"\t{lvl_tx}\t{lvl_rx}\t{lvl_sdr1}\t{lvl_sdr2}\t{lvl_sv}"
                f"\t{q_aioc}\t{q_sdr1}\t{q_sdr2}"
                f"\t{ptt}\t{vad}\t{rebro}\t{rss_mb:.1f}\n")
        buffer.append(line)

        # Flush to disk periodically
        if now_mono - last_flush >= FLUSH_INTERVAL and buffer:
            try:
                with open(out_path, 'a') as f:
                    f.writelines(buffer)
                buffer.clear()
                last_flush = now_mono
            except Exception:
                pass

    # Final flush on stop
    if buffer:
        try:
            with open(out_path, 'a') as f:
                f.write(f"# Watchdog stopped {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.writelines(buffer)
        except Exception:
            pass


def dump_audio_trace(gw):
    """Write audio trace to tools/audio_trace.txt on shutdown."""
    from gateway_core import __version__
    trace = list(gw._audio_trace)
    if not trace:
        return
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'audio_trace.txt')
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
    except Exception:
        pass

    # Column indices
    T, DT, SQ, SSB, AQ, ASB, MGOT, MSRC, MMS, SBLK, ABLK, \
        OUTCOME, MUMMS, SPKOK, SPKQD, DRMS, DLEN, MXST, \
        SQ2, SSB2, SPREBUF, S2PREBUF, REBRO, SVMS, SVSENT, \
        SDR1_DISC, SDR1_SBA, SDR1_OVF, SDR1_DROP, \
        AIOC_DISC, AIOC_SBA, AIOC_OVF, AIOC_DROP, \
        OUT_DISC, \
        KV4P_RXF, KV4P_RXB, KV4P_QDROP, KV4P_SBB, KV4P_SBA, \
        KV4P_GOT, KV4P_RMS, KV4P_QLEN, KV4P_DECERR, \
        KV4P_TXF, KV4P_TXDROP, KV4P_TXRMS, KV4P_TXERR, KV4P_TXANN, \
        SDR2_DISC, SDR2_SBA = range(50)

    with open(out_path, 'w') as f:
        dur = trace[-1][T] - trace[0][T] if len(trace) > 1 else 0
        f.write(f"Audio Trace: {len(trace)} ticks, {dur:.1f}s\n")
        f.write(f"{'='*90}\n\n")

        # ── System info ──
        sdr_mode = "SDRPlugin" if gw.sdr_plugin else "none"
        f.write("SYSTEM\n")
        f.write(f"  version={__version__}\n")
        f.write(f"  os={platform.system()} {platform.release()} arch={platform.machine()}\n")
        f.write(f"  python={platform.python_version()} sdr_mode={sdr_mode}\n")
        f.write(f"  host={platform.node()}\n\n")

        # ── Summary statistics ──
        dts = [r[DT] for r in trace]
        mixer_got = sum(1 for r in trace if r[MGOT])
        mixer_none = len(trace) - mixer_got
        mixer_ms = [r[MMS] for r in trace]
        sdr_blocked = [r[SBLK] for r in trace if r[SBLK] > 0]
        aioc_blocked = [r[ABLK] for r in trace if r[ABLK] > 0]

        f.write("TICK TIMING (target: 50.0ms)\n")
        f.write(f"  count={len(dts)}  mean={statistics.mean(dts):.1f}ms  "
                f"stdev={statistics.stdev(dts):.1f}ms  min={min(dts):.1f}ms  max={max(dts):.1f}ms\n")
        over_60 = sum(1 for d in dts if d > 60)
        over_80 = sum(1 for d in dts if d > 80)
        over_100 = sum(1 for d in dts if d > 100)
        f.write(f"  >60ms: {over_60}  >80ms: {over_80}  >100ms: {over_100}\n\n")

        f.write("MIXER OUTPUT\n")
        f.write(f"  audio: {mixer_got} ({100*mixer_got/len(trace):.1f}%)  "
                f"silence: {mixer_none} ({100*mixer_none/len(trace):.1f}%)\n")
        f.write(f"  call time: mean={statistics.mean(mixer_ms):.2f}ms  max={max(mixer_ms):.2f}ms\n\n")

        # Source breakdown
        src_counts = {}
        for r in trace:
            key = r[MSRC] if r[MSRC] else '(none)'
            src_counts[key] = src_counts.get(key, 0) + 1
        f.write("SOURCE BREAKDOWN\n")
        for src, cnt in sorted(src_counts.items(), key=lambda x: -x[1]):
            f.write(f"  {src}: {cnt} ({100*cnt/len(trace):.1f}%)\n")
        f.write("\n")

        # ── Downstream outcome ──
        outcome_counts = {}
        for r in trace:
            o = r[OUTCOME]
            outcome_counts[o] = outcome_counts.get(o, 0) + 1
        f.write("DOWNSTREAM OUTCOME\n")
        for o, cnt in sorted(outcome_counts.items(), key=lambda x: -x[1]):
            f.write(f"  {o}: {cnt} ({100*cnt/len(trace):.1f}%)\n")
        f.write("\n")

        # Mumble send timing
        mumble_ms = [r[MUMMS] for r in trace if r[OUTCOME] == 'sent' and r[MUMMS] > 0]
        if mumble_ms:
            f.write(f"MUMBLE add_sound()\n")
            f.write(f"  count={len(mumble_ms)}  mean={statistics.mean(mumble_ms):.2f}ms  max={max(mumble_ms):.2f}ms\n\n")

        sv_ms_vals = [r[SVMS] for r in trace if len(r) > SVMS and r[SVSENT] > 0]
        if sv_ms_vals:
            f.write(f"REMOTE AUDIO SERVER send_audio()\n")
            f.write(f"  ticks with sends={len(sv_ms_vals)}/{len(trace)}  "
                    f"mean={statistics.mean(sv_ms_vals):.2f}ms  max={max(sv_ms_vals):.2f}ms\n")
            sv_slow = sum(1 for v in sv_ms_vals if v > 5.0)
            sv_vslow = sum(1 for v in sv_ms_vals if v > 50.0)
            f.write(f"  >5ms: {sv_slow}  >50ms: {sv_vslow}\n\n")

        # Speaker queue
        spk_qds = [r[SPKQD] for r in trace if r[SPKQD] >= 0]
        if spk_qds:
            f.write(f"SPEAKER QUEUE DEPTH (at enqueue time)\n")
            f.write(f"  min={min(spk_qds)}  mean={statistics.mean(spk_qds):.1f}  max={max(spk_qds)}\n")
            spk_full = sum(1 for q in spk_qds if q >= 8)
            f.write(f"  full (>=8): {spk_full} ({100*spk_full/len(spk_qds):.1f}%)\n\n")

        # Data RMS
        rms_vals = [r[DRMS] for r in trace if r[DRMS] > 0]
        if rms_vals:
            f.write(f"DATA RMS (non-zero only)\n")
            f.write(f"  count={len(rms_vals)}/{len(trace)}  mean={statistics.mean(rms_vals):.0f}  "
                    f"min={min(rms_vals):.0f}  max={max(rms_vals):.0f}\n")
            zero_rms = sum(1 for r in trace if r[OUTCOME] == 'sent' and r[DRMS] == 0)
            f.write(f"  sent with RMS=0 (silence): {zero_rms}\n\n")

        # Data length
        dlens = [r[DLEN] for r in trace if r[DLEN] > 0]
        if dlens:
            expected = gw.config.AUDIO_CHUNK_SIZE * 2  # 4800 bytes for 50ms mono
            wrong = sum(1 for d in dlens if d != expected)
            f.write(f"DATA LENGTH (expected: {expected} bytes = {gw.config.AUDIO_CHUNK_SIZE} frames)\n")
            f.write(f"  count={len(dlens)}/{len(trace)}  min={min(dlens)}  max={max(dlens)}\n")
            if wrong:
                f.write(f"  *** WRONG SIZE: {wrong} chunks ({100*wrong/len(dlens):.1f}%) ***\n")
                sizes = {}
                for d in dlens:
                    sizes[d] = sizes.get(d, 0) + 1
                f.write(f"  size distribution: {dict(sorted(sizes.items()))}\n")
            f.write("\n")

        if sdr_blocked:
            f.write(f"SDR BLOB FETCH (blocked {len(sdr_blocked)}/{len(trace)} ticks)\n")
            f.write(f"  mean={statistics.mean(sdr_blocked):.1f}ms  max={max(sdr_blocked):.1f}ms\n\n")
        else:
            f.write("SDR BLOB FETCH: never blocked\n\n")

        if aioc_blocked:
            f.write(f"AIOC BLOB FETCH (blocked {len(aioc_blocked)}/{len(trace)} ticks)\n")
            f.write(f"  mean={statistics.mean(aioc_blocked):.1f}ms  max={max(aioc_blocked):.1f}ms\n\n")
        else:
            f.write("AIOC BLOB FETCH: never blocked\n\n")

        # SDR1 queue depth
        sq_vals = [r[SQ] for r in trace if r[SQ] >= 0]
        if sq_vals:
            f.write(f"SDR1 QUEUE DEPTH\n")
            f.write(f"  min={min(sq_vals)}  mean={statistics.mean(sq_vals):.1f}  max={max(sq_vals)}\n")
            n = len(sq_vals)
            q1 = statistics.mean(sq_vals[:n//4]) if n >= 4 else 0
            q4 = statistics.mean(sq_vals[-n//4:]) if n >= 4 else 0
            f.write(f"  first quarter={q1:.1f}  last quarter={q4:.1f}\n")
            pb_ticks = sum(1 for r in trace if len(r) > SPREBUF and r[SPREBUF])
            f.write(f"  prebuffering: {pb_ticks}/{len(trace)} ticks\n")
            plc_total = 0
            f.write(f"  PLC repeats: {plc_total} (gap concealment)\n\n")

        # SDR2 queue depth
        sq2_vals = [r[SQ2] for r in trace if len(r) > SQ2 and r[SQ2] >= 0]
        if sq2_vals:
            f.write(f"SDR2 QUEUE DEPTH\n")
            f.write(f"  min={min(sq2_vals)}  mean={statistics.mean(sq2_vals):.1f}  max={max(sq2_vals)}\n")
            pb2_ticks = sum(1 for r in trace if len(r) > S2PREBUF and r[S2PREBUF])
            f.write(f"  prebuffering: {pb2_ticks}/{len(trace)} ticks\n")
            plc2_total = 0
            f.write(f"  PLC repeats: {plc2_total} (gap concealment)\n\n")

        # AIOC queue depth
        aq_vals = [r[AQ] for r in trace if r[AQ] >= 0]
        if aq_vals:
            f.write(f"AIOC QUEUE DEPTH\n")
            f.write(f"  min={min(aq_vals)}  mean={statistics.mean(aq_vals):.1f}  max={max(aq_vals)}\n\n")

        # ── PortAudio callback health ──
        has_enhanced = len(trace[0]) > SDR1_DISC if trace else False
        if has_enhanced:
            # SDR1 callback stats (cumulative — use last tick's values)
            last = trace[-1]
            sdr1_ovf_total = last[SDR1_OVF] if last[SDR1_OVF] else 0
            sdr1_drop_total = last[SDR1_DROP] if last[SDR1_DROP] else 0
            aioc_ovf_total = last[AIOC_OVF] if last[AIOC_OVF] else 0
            aioc_drop_total = last[AIOC_DROP] if last[AIOC_DROP] else 0

            f.write("PORTAUDIO CALLBACK HEALTH\n")
            f.write(f"  SDR1: overflows={sdr1_ovf_total}  queue_drops={sdr1_drop_total}\n")
            f.write(f"  AIOC: overflows={aioc_ovf_total}  queue_drops={aioc_drop_total}\n")
            if sdr1_ovf_total or sdr1_drop_total:
                f.write(f"  *** SDR1 callback issues detected — data may be lost ***\n")
            if aioc_ovf_total or aioc_drop_total:
                f.write(f"  *** AIOC callback issues detected — data may be lost ***\n")
            f.write("\n")

            # Sample discontinuities
            sdr1_discs = [r[SDR1_DISC] for r in trace if r[SDR1_DISC] > 0]
            sdr2_discs = [r[SDR2_DISC] for r in trace if len(r) > SDR2_DISC and r[SDR2_DISC] > 0]
            aioc_discs = [r[AIOC_DISC] for r in trace if r[AIOC_DISC] > 0]

            f.write("SAMPLE DISCONTINUITIES (inter-chunk boundary jumps)\n")
            f.write("  (Large jumps between last sample of chunk N and first sample of chunk N+1 cause clicks)\n")
            if sdr1_discs:
                big_jumps = [d for d in sdr1_discs if d > 1000]
                huge_jumps = [d for d in sdr1_discs if d > 5000]
                f.write(f"  SDR1: count={len(sdr1_discs)}/{len(trace)}  "
                        f"mean={statistics.mean(sdr1_discs):.0f}  max={max(sdr1_discs):.0f}  "
                        f">1000: {len(big_jumps)}  >5000: {len(huge_jumps)}\n")
            else:
                f.write("  SDR1: no discontinuities (all chunks zero or no audio)\n")
            if sdr2_discs:
                big_jumps = [d for d in sdr2_discs if d > 1000]
                huge_jumps = [d for d in sdr2_discs if d > 5000]
                f.write(f"  SDR2: count={len(sdr2_discs)}/{len(trace)}  "
                        f"mean={statistics.mean(sdr2_discs):.0f}  max={max(sdr2_discs):.0f}  "
                        f">1000: {len(big_jumps)}  >5000: {len(huge_jumps)}\n")
            else:
                f.write("  SDR2: no discontinuities (all chunks zero or no audio)\n")
            if aioc_discs:
                big_jumps = [d for d in aioc_discs if d > 1000]
                huge_jumps = [d for d in aioc_discs if d > 5000]
                f.write(f"  AIOC: count={len(aioc_discs)}/{len(trace)}  "
                        f"mean={statistics.mean(aioc_discs):.0f}  max={max(aioc_discs):.0f}  "
                        f">1000: {len(big_jumps)}  >5000: {len(huge_jumps)}\n")
            else:
                f.write("  AIOC: no discontinuities (all chunks zero or no audio)\n")

            # Output-side discontinuities (after mixer — what Mumble actually gets)
            out_discs = [r[OUT_DISC] for r in trace if len(r) > OUT_DISC and r[OUT_DISC] > 0]
            if out_discs:
                big_jumps = [d for d in out_discs if d > 1000]
                huge_jumps = [d for d in out_discs if d > 5000]
                f.write(f"  OUTPUT (mixer→Mumble): count={len(out_discs)}/{len(trace)}  "
                        f"mean={statistics.mean(out_discs):.0f}  max={max(out_discs):.0f}  "
                        f">1000: {len(big_jumps)}  >5000: {len(huge_jumps)}\n")
                if huge_jumps:
                    f.write(f"  *** {len(huge_jumps)} output clicks detected (>5000 sample jump) ***\n")
            else:
                f.write("  OUTPUT: no discontinuities\n")
            f.write("\n")

            # Sub-buffer after-serve levels
            sdr1_sba = [r[SDR1_SBA] for r in trace if r[SDR1_SBA] >= 0]
            aioc_sba = [r[AIOC_SBA] for r in trace if r[AIOC_SBA] >= 0]
            if sdr1_sba:
                near_empty = sum(1 for s in sdr1_sba if s < gw.config.AUDIO_CHUNK_SIZE * 2)
                f.write(f"SDR1 SUB-BUFFER AFTER SERVE\n")
                f.write(f"  min={min(sdr1_sba)}  mean={statistics.mean(sdr1_sba):.0f}  max={max(sdr1_sba)}\n")
                f.write(f"  near-empty (<1 chunk): {near_empty}/{len(sdr1_sba)} "
                        f"({100*near_empty/len(sdr1_sba):.1f}%) — next tick may deplete\n\n")
            if aioc_sba:
                near_empty = sum(1 for s in aioc_sba if s < gw.config.AUDIO_CHUNK_SIZE * 2)
                f.write(f"AIOC SUB-BUFFER AFTER SERVE\n")
                f.write(f"  min={min(aioc_sba)}  mean={statistics.mean(aioc_sba):.0f}  max={max(aioc_sba)}\n")
                f.write(f"  near-empty (<1 chunk): {near_empty}/{len(aioc_sba)} "
                        f"({100*near_empty/len(aioc_sba):.1f}%) — next tick may deplete\n\n")

        # ── Gap analysis ──
        gaps = []
        g = 0
        for r in trace:
            if not r[MGOT]:
                g += 1
            else:
                if g > 0:
                    gaps.append(g)
                g = 0
        if g > 0:
            gaps.append(g)
        if gaps:
            gap_ms = [x * 50 for x in gaps]
            f.write(f"SILENCE GAPS (mixer): {len(gaps)} gaps\n")
            f.write(f"  sizes (ticks): {gaps[:50]}\n")
            f.write(f"  max gap: {max(gap_ms)}ms\n\n")
        else:
            f.write("SILENCE GAPS (mixer): none\n\n")

        # ── Mixer state summary ──
        has_state = any(r[MXST] for r in trace)
        if has_state:
            ducked_count = sum(1 for r in trace if r[MXST].get('dk', False))
            hold_count = sum(1 for r in trace if r[MXST].get('hold', False))
            pad_count = sum(1 for r in trace if r[MXST].get('pad', False))
            tOut_count = sum(1 for r in trace if r[MXST].get('tOut', False))
            ducks_count = sum(1 for r in trace if r[MXST].get('ducks', False))
            radio_sig_count = sum(1 for r in trace if r[MXST].get('radioSig', False))
            oaa_count = sum(1 for r in trace if r[MXST].get('oaa', False))
            n = len(trace)
            f.write("MIXER STATE\n")
            f.write(f"  ducked: {ducked_count}/{n} ({100*ducked_count/n:.1f}%)  "
                    f"hold_fired: {hold_count}/{n} ({100*hold_count/n:.1f}%)  "
                    f"padding: {pad_count}/{n} ({100*pad_count/n:.1f}%)\n")
            f.write(f"  trans_out: {tOut_count}/{n} ({100*tOut_count/n:.1f}%)  "
                    f"aioc_ducks: {ducks_count}/{n} ({100*ducks_count/n:.1f}%)  "
                    f"radio_signal: {radio_sig_count}/{n} ({100*radio_sig_count/n:.1f}%)\n")
            f.write(f"  other_audio_active: {oaa_count}/{n} ({100*oaa_count/n:.1f}%)\n")

            # Per-SDR state summary
            sdr_names = set()
            for r in trace:
                sdr_names.update(r[MXST].get('sdrs', {}).keys())
            for sname in sorted(sdr_names):
                s_ducked = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('ducked', False))
                s_inc = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('inc', False))
                s_sig = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('sig', False))
                s_hold = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('hold', False))
                s_sole = sum(1 for r in trace if r[MXST].get('sdrs', {}).get(sname, {}).get('sole', False))
                f.write(f"  {sname}: ducked={s_ducked}  included={s_inc}  signal={s_sig}  "
                        f"hold={s_hold}  sole_source={s_sole}\n")

            # Mute state (usually constant, just show if any were active)
            rx_m = sum(1 for r in trace if r[MXST].get('rx_m', False))
            gl_m = sum(1 for r in trace if r[MXST].get('gl_m', False))
            sp_m = sum(1 for r in trace if r[MXST].get('sp_m', False))
            mutes = []
            if rx_m: mutes.append(f"rx_muted={rx_m}")
            if gl_m: mutes.append(f"global_muted={gl_m}")
            if sp_m: mutes.append(f"speaker_muted={sp_m}")
            if mutes:
                f.write(f"  mutes: {', '.join(mutes)}\n")
            f.write("\n")

        # ── Duck-release analysis ──
        # For each SDR: find every tick where the SDR went from ducked→not-ducked.
        # Check: did fade-in fire on the release tick (or next tick)?
        # Also check: was the queue depth reasonable at release?
        duck_release_events = []
        for i in range(1, len(trace)):
            prev_r = trace[i-1]
            curr_r = trace[i]
            if not (len(prev_r) > MXST and len(curr_r) > MXST):
                continue
            prev_st = prev_r[MXST] or {}
            curr_st = curr_r[MXST] or {}
            for sname in sorted((prev_st.get('sdrs', {}) | curr_st.get('sdrs', {})).keys()):
                prev_s = prev_st.get('sdrs', {}).get(sname, {})
                curr_s = curr_st.get('sdrs', {}).get(sname, {})
                if prev_s.get('ducked') and not curr_s.get('ducked'):
                    # Duck just released for this SDR
                    sdr_q = curr_r[SQ] if sname == 'SDR1' else (curr_r[SQ2] if len(curr_r) > SQ2 else -1)
                    fi_fired = curr_s.get('fi', False)
                    inc = curr_s.get('inc', False)
                    # Missing fade-in: included on release tick but prev_included was True
                    # (fade-in should always fire after a duck due to our reset fix)
                    missing_fi = inc and not fi_fired
                    duck_release_events.append({
                        'tick': i, 't': curr_r[T], 'sdr': sname,
                        'q': sdr_q, 'fi': fi_fired, 'inc': inc, 'missing_fi': missing_fi,
                    })

        if duck_release_events:
            f.write("DUCK RELEASE EVENTS\n")
            for ev in duck_release_events:
                fi_str = 'fade-in=YES' if ev['fi'] else ('fade-in=MISSING!' if ev['missing_fi'] else 'fade-in=no(not-inc)')
                f.write(f"  tick {ev['tick']:4d}  t={ev['t']:.3f}s  {ev['sdr']}  "
                        f"q={ev['q']}  inc={ev['inc']}  {fi_str}\n")
            missing = [ev for ev in duck_release_events if ev['missing_fi']]
            if missing:
                f.write(f"  *** {len(missing)} duck release(s) WITHOUT fade-in — SDR resumed at full volume → click risk ***\n")
            else:
                f.write(f"  All {len(duck_release_events)} duck release(s) had correct fade-in.\n")
            f.write("\n")
        else:
            f.write("DUCK RELEASE EVENTS: none (no duck→unduck transitions observed)\n\n")

        # ── Gap-stutter analysis ──
        gap_stutter_ticks = [
            (i, r) for i, r in enumerate(trace)
            if len(r) > MXST and r[MXST]
            and r[MXST].get('dk') and not r[MXST].get('ducks')
            and r[MXST].get('oaa') and r[MXST].get('nptt_none')
        ]
        if gap_stutter_ticks:
            f.write(f"GAP-STUTTER EVENTS (is_ducked=T, aioc_ducks=F, oaa=T, aioc_gap=T)\n")
            f.write(f"  *** {len(gap_stutter_ticks)} ticks where AIOC blob gap briefly un-ducked SDR ***\n")
            f.write(f"  These are the cause of SDR stutter during AIOC transmission.\n")
            f.write(f"  First occurrence: tick {gap_stutter_ticks[0][0]}  t={gap_stutter_ticks[0][1][0]:.3f}s\n")
            # Show run-lengths (how many consecutive gap-stutter ticks)
            runs = []
            run_start = gap_stutter_ticks[0][0]
            run_len = 1
            for k in range(1, len(gap_stutter_ticks)):
                if gap_stutter_ticks[k][0] == gap_stutter_ticks[k-1][0] + 1:
                    run_len += 1
                else:
                    runs.append((run_start, run_len))
                    run_start = gap_stutter_ticks[k][0]
                    run_len = 1
            runs.append((run_start, run_len))
            f.write(f"  Gap bursts (tick, length): {runs[:20]}\n")
            f.write(f"  Total gap-stutter ticks: {len(gap_stutter_ticks)} (~{len(gap_stutter_ticks)*50}ms of SDR bleed-through)\n\n")
        else:
            f.write("GAP-STUTTER EVENTS: none detected\n\n")

        # ── Rebroadcast summary ──
        rebro_vals = [r[REBRO] for r in trace if len(r) > REBRO and r[REBRO]]
        if rebro_vals:
            n = len(trace)
            r_sig = sum(1 for v in rebro_vals if v == 'sig')
            r_hold = sum(1 for v in rebro_vals if v == 'hold')
            r_idle = sum(1 for v in rebro_vals if v == 'idle')
            f.write("SDR REBROADCAST\n")
            f.write(f"  active: {len(rebro_vals)}/{n} ticks  "
                    f"sig={r_sig} ({100*r_sig/n:.1f}%)  "
                    f"hold={r_hold} ({100*r_hold/n:.1f}%)  "
                    f"idle={r_idle} ({100*r_idle/n:.1f}%)\n\n")

        # ── Per-tick detail (first 200 + any anomalies) ──
        #
        # Mixer state column legend:
        #   D=ducked H=hold P=padding T=trans_out A=aioc_ducks R=radio_sig O=other_active N=aioc_gap(nptt_none)
        #   Per SDR: D=ducked S=signal H=hold X=sole .=excluded I=included(no signal)
        #   GAP-STUTTER: D=True, A=False, O=True, N=True → is_ducked but AIOC gap un-ducked SDR
        def _fmt_mxst(st):
            """Format mixer state dict into compact string."""
            if not st:
                return ''
            flags = ''
            flags += 'D' if st.get('dk') else '-'
            flags += 'H' if st.get('hold') else '-'
            flags += 'P' if st.get('pad') else '-'
            flags += 'T' if st.get('tOut') else '-'
            flags += 'A' if st.get('ducks') else '-'
            flags += 'R' if st.get('radioSig') else '-'
            flags += 'O' if st.get('oaa') else '-'
            flags += 'N' if st.get('nptt_none') else '-'
            flags += 'I' if st.get('ri') else '-'
            sdrs = st.get('sdrs', {})
            for sname in sorted(sdrs.keys()):
                s = sdrs[sname]
                flags += ' '
                if s.get('ducked'):
                    flags += 'D'
                elif s.get('fi'):
                    flags += 'F'  # fade-in fired (first inclusion after silence/duck)
                elif s.get('fo'):
                    flags += 'O'  # fade-out fired (last frame before going silent)
                elif s.get('inc'):
                    if s.get('sig'):
                        flags += 'S'
                    elif s.get('hold'):
                        flags += 'H'
                    elif s.get('sole'):
                        flags += 'X'
                    else:
                        flags += 'I'
                else:
                    flags += '.'
            return flags

        # ── Reader blob delivery intervals ──
        for src_name, src_obj in [('SDR1', gw.sdr_plugin.get_tuner(1) if gw.sdr_plugin else None), ('SDR2', gw.sdr_plugin.get_tuner(2) if gw.sdr_plugin else None)]:
            if src_obj and getattr(src_obj, '_blob_times', None):
                btimes = list(src_obj._blob_times)
                if len(btimes) > 1:
                    intervals = [(btimes[k+1] - btimes[k]) * 1000 for k in range(len(btimes)-1)]
                    f.write(f"\n{src_name} READER BLOB DELIVERY INTERVALS ({len(intervals)} gaps)\n")
                    f.write(f"  mean={statistics.mean(intervals):.0f}ms  "
                            f"stdev={statistics.stdev(intervals):.0f}ms  "
                            f"min={min(intervals):.0f}ms  max={max(intervals):.0f}ms\n")
                    late = [iv for iv in intervals if iv > 500]
                    if late:
                        f.write(f"  >500ms stalls: {len(late)} — max={max(late):.0f}ms\n")
                else:
                    f.write(f"\n{src_name} READER BLOB DELIVERY: too few samples\n")

        f.write(f"\n{'='*140}\n")
        f.write("PER-TICK DETAIL (all ticks; * = anomaly)\n")
        f.write(f"{'='*140}\n")
        f.write("  State: D=ducked H=hold P=padding T=trans_out A=aioc_ducks R=radio_sig O=other_active N=aioc_gap I=reduck_inhibit\n")
        f.write("  * GAP-STUTTER tick: D=T A=F O=T N=T → is_ducked but AIOC blob gap caused SDR to briefly un-duck\n")
        f.write("  SDR:   D=ducked F=fade-in(first-inc) O=fade-out(going-silent) S=signal H=hold_inc X=sole_src I=inc(other) .=excluded\n")
        f.write("  * MISSING-FADE-IN tick: SDR included at duck-release without fade-in → click risk\n")
        f.write("  PB: B=prebuffering (waiting to rebuild cushion) .=normal\n")
        f.write("  RB: sig=rebroadcast sending  hold=PTT hold  idle=on but no signal\n")
        f.write("  s1_disc/s2_disc/a_disc/o_disc: sample discontinuity at chunk boundary (abs delta, >5000=click)\n")
        f.write("  s1_sba/a_sba: sub-buffer bytes remaining AFTER serving this chunk\n")
        f.write("  kv_txf: TX Opus frames sent | kv_txdrop: TX PCM bytes dropped (partial frame) | kv_txrms: TX input RMS | kv_ann: TX silenced by PTT settle delay\n\n")
        _missing_fi_ticks = {ev['tick'] for ev in duck_release_events if ev['missing_fi']}
        _duck_release_ticks = {ev['tick'] for ev in duck_release_events}

        hdr = (f"{'tick':>6} {'t(s)':>7} {'dt':>6} "
               f"{'s1_q':>4} {'s1_sb':>6} {'s1_sba':>6} {'s2_q':>4} {'s2_sb':>6} {'pb':>2} "
               f"{'aioc_q':>6} {'aioc_sb':>7} {'a_sba':>6} {'mixer':>5} {'mix_ms':>6} "
               f"{'outcome':>10} {'m_ms':>5} {'spk_q':>5} {'rms':>7} {'dlen':>5} "
               f"{'sv_ms':>6} {'sv#':>3} "
               f"{'s1_disc':>7} {'s2_disc':>7} {'a_disc':>7} {'o_disc':>7} "
               f"{'kv_rxf':>6} {'kv_rxB':>6} {'kv_qd':>5} {'kv_sbb':>7} {'kv_sba':>7} {'kv_got':>6} {'kv_rms':>7} {'kv_q':>4} "
               f"{'kv_txf':>6} {'kv_txdrop':>9} {'kv_txrms':>8} {'kv_txerr':>8} {'kv_ann':>6} "
               f"{'sources':>14} {'state':>14} {'rb':>4}\n")
        f.write(hdr)
        f.write('-' * len(hdr) + '\n')
        for i, r in enumerate(trace):
            expected_len = gw.config.AUDIO_CHUNK_SIZE * 2
            _has_enh = len(r) > SDR1_DISC
            _st = r[MXST] if len(r) > MXST and r[MXST] else {}
            # Gap-stutter event: is_ducked=True but aioc_ducks_sdrs=False because
            # AIOC had a blob gap this tick (nptt_none=True) — SDR briefly un-ducked
            _gap_stutter = (_st.get('dk') and not _st.get('ducks')
                            and _st.get('oaa') and _st.get('nptt_none'))
            # SDR queue unexpectedly large: means get_audio() was not draining
            # it during a duck, so stale buffered audio will play at release.
            _sdr_q_spike = r[SQ] > 8 or (len(r) > SQ2 and r[SQ2] > 8)
            is_anomaly = (r[DT] > 80 or not r[MGOT] or r[MMS] > 20
                          or r[OUTCOME] not in ('sent', 'mix')
                          or r[MUMMS] > 5 or r[SPKQD] >= 7 or r[DRMS] == 0
                          or (r[DLEN] > 0 and r[DLEN] != expected_len)
                          or (len(r) > SPREBUF and (r[SPREBUF] or r[S2PREBUF]))
                          or (len(r) > SVMS and r[SVMS] > 5.0)
                          or (_has_enh and r[SDR1_DISC] > 5000)
                          or (_has_enh and r[AIOC_DISC] > 5000)
                          or (len(r) > OUT_DISC and r[OUT_DISC] > 5000)
                          or (len(r) > SDR2_DISC and r[SDR2_DISC] > 5000)
                          or _gap_stutter
                          or i in _missing_fi_ticks
                          or _sdr_q_spike)
            flag = '*' if is_anomaly else ' '
            st = _fmt_mxst(r[MXST]) if len(r) > MXST else ''
            sq2 = r[SQ2] if len(r) > SQ2 else -1
            ssb2 = r[SSB2] if len(r) > SSB2 else -1
            pb1 = 'B' if (len(r) > SPREBUF and r[SPREBUF]) else '.'
            pb2 = 'B' if (len(r) > S2PREBUF and r[S2PREBUF]) else '.'
            rb = r[REBRO] if len(r) > REBRO else ''
            sv_ms = r[SVMS] if len(r) > SVMS else 0.0
            sv_n = r[SVSENT] if len(r) > SVSENT else 0
            s1_disc = r[SDR1_DISC] if _has_enh else 0.0
            s1_sba = r[SDR1_SBA] if _has_enh else -1
            a_disc = r[AIOC_DISC] if _has_enh else 0.0
            a_sba = r[AIOC_SBA] if _has_enh else -1
            o_disc = r[OUT_DISC] if (len(r) > OUT_DISC) else 0.0
            s2_disc = r[SDR2_DISC] if len(r) > SDR2_DISC else 0.0
            _kv = len(r) > KV4P_RXF
            kv_rxf = r[KV4P_RXF] if _kv else 0
            kv_rxB = r[KV4P_RXB] if _kv else 0
            kv_qd = r[KV4P_QDROP] if _kv else 0
            kv_sbb = r[KV4P_SBB] if _kv else 0
            kv_sba = r[KV4P_SBA] if _kv else 0
            kv_got = r[KV4P_GOT] if _kv else False
            kv_rms = r[KV4P_RMS] if _kv else 0.0
            kv_q = r[KV4P_QLEN] if _kv else 0
            _kv_tx = len(r) > KV4P_TXF
            kv_txf = r[KV4P_TXF] if _kv_tx else 0
            kv_txdrop = r[KV4P_TXDROP] if _kv_tx else 0
            kv_txrms = r[KV4P_TXRMS] if _kv_tx else 0.0
            kv_txerr = r[KV4P_TXERR] if _kv_tx else 0
            kv_ann = 'Y' if (_kv_tx and r[KV4P_TXANN]) else '.'
            f.write(f"{i:>5}{flag} {r[T]:7.3f} {r[DT]:6.1f} "
                    f"{r[SQ]:4} {r[SSB]:6} {s1_sba:6} {sq2:4} {ssb2:6} {pb1}{pb2} "
                    f"{r[AQ]:6} {r[ASB]:7} {a_sba:6} {'audio' if r[MGOT] else 'NONE':>5} "
                    f"{r[MMS]:6.1f} "
                    f"{r[OUTCOME]:>10} {r[MUMMS]:5.1f} {r[SPKQD]:5} {r[DRMS]:7.0f} "
                    f"{r[DLEN]:5} {sv_ms:6.1f} {sv_n:3} "
                    f"{s1_disc:7.0f} {s2_disc:7.0f} {a_disc:7.0f} {o_disc:7.0f} "
                    f"{kv_rxf:6} {kv_rxB:6} {kv_qd:5} {kv_sbb:7} {kv_sba:7} {'yes' if kv_got else 'no':>6} {kv_rms:7.0f} {kv_q:4} "
                    f"{kv_txf:6} {kv_txdrop:9} {kv_txrms:8.0f} {kv_txerr:8} {kv_ann:>6} "
                    f"{r[MSRC]:>14} {st} {rb:>4}\n")

        # ── KV4P summary ──
        kv4p_ticks = [r for r in trace if len(r) > KV4P_RXF]
        if kv4p_ticks:
            kv_got = sum(1 for r in kv4p_ticks if r[KV4P_GOT])
            kv_none = len(kv4p_ticks) - kv_got
            kv_drops = sum(r[KV4P_QDROP] for r in kv4p_ticks)
            kv_rxf_total = sum(r[KV4P_RXF] for r in kv4p_ticks)
            kv_rxB_total = sum(r[KV4P_RXB] for r in kv4p_ticks)
            kv_sbb_vals = [r[KV4P_SBB] for r in kv4p_ticks]
            kv_rms_vals = [r[KV4P_RMS] for r in kv4p_ticks if r[KV4P_GOT]]
            kv_decerr = sum(r[KV4P_DECERR] for r in kv4p_ticks)

            f.write(f"\n{'='*90}\n")
            f.write("KV4P AUDIO\n")
            f.write(f"{'='*90}\n")
            f.write(f"  ticks={len(kv4p_ticks)}  data={kv_got} ({kv_got*100//max(1,len(kv4p_ticks))}%)  "
                    f"underrun={kv_none} ({kv_none*100//max(1,len(kv4p_ticks))}%)\n")
            f.write(f"  opus_frames={kv_rxf_total}  opus_bytes={kv_rxB_total}  "
                    f"queue_drops={kv_drops}  decode_errors={kv_decerr}\n")
            if kv_sbb_vals:
                f.write(f"  sub_buf: mean={statistics.mean(kv_sbb_vals):.0f}B  "
                        f"min={min(kv_sbb_vals)}B  max={max(kv_sbb_vals)}B\n")
            if kv_rms_vals:
                f.write(f"  rms: mean={statistics.mean(kv_rms_vals):.0f}  "
                        f"min={min(kv_rms_vals):.0f}  max={max(kv_rms_vals):.0f}\n")
            # Identify gap patterns: consecutive underruns
            gaps = []
            gap_len = 0
            for r in kv4p_ticks:
                if not r[KV4P_GOT]:
                    gap_len += 1
                else:
                    if gap_len > 0:
                        gaps.append(gap_len)
                    gap_len = 0
            if gap_len > 0:
                gaps.append(gap_len)
            if gaps:
                f.write(f"  gap_runs={len(gaps)}  gap_ticks: mean={statistics.mean(gaps):.1f}  "
                        f"max={max(gaps)}  total={sum(gaps)}\n")

            # TX summary
            tx_ticks = [r for r in kv4p_ticks if len(r) > KV4P_TXF and r[KV4P_TXF] > 0]
            ann_ticks = sum(1 for r in kv4p_ticks if len(r) > KV4P_TXANN and r[KV4P_TXANN])
            tx_frames_total = sum(r[KV4P_TXF] for r in kv4p_ticks if len(r) > KV4P_TXF)
            tx_drop_total = sum(r[KV4P_TXDROP] for r in kv4p_ticks if len(r) > KV4P_TXDROP)
            tx_err_total = sum(r[KV4P_TXERR] for r in kv4p_ticks if len(r) > KV4P_TXERR)
            tx_rms_vals = [r[KV4P_TXRMS] for r in tx_ticks if r[KV4P_TXRMS] > 0]
            f.write(f"\n  TX (gateway→radio):\n")
            f.write(f"    ticks_with_tx={len(tx_ticks)}  frames_sent={tx_frames_total}  "
                    f"buf_carry={tx_drop_total}  encode_errors={tx_err_total}  ann_delay_ticks={ann_ticks}\n")
            if tx_rms_vals:
                f.write(f"    input_rms: mean={statistics.mean(tx_rms_vals):.0f}  "
                        f"min={min(tx_rms_vals):.0f}  max={max(tx_rms_vals):.0f}\n")
            if tx_frames_total > 0:
                audio_sent = tx_frames_total * 3840
                audio_in = len(tx_ticks) * 4800
                sent_pct = audio_sent * 100 // max(1, audio_in)
                f.write(f"    audio_sent={audio_sent}B ({sent_pct}% of {audio_in}B input)  "
                        f"buf_carry is bytes held across ticks, not dropped\n")
            f.write("\n")

        # ── Events (key presses / mode changes) ──
        events = list(gw._trace_events)
        if events:
            f.write(f"\n{'='*90}\n")
            f.write(f"EVENTS ({len(events)})\n")
            f.write(f"{'='*90}\n")
            for ts, etype, evalue in events:
                rel = ts - gw._audio_trace_t0 if gw._audio_trace_t0 > 0 else 0
                f.write(f"  {rel:8.3f}s  {etype:<15} {evalue}\n")

        # ── Speaker thread trace ──
        spk = list(gw._spk_trace)
        if spk:
            ST, SWAIT, SWR, SQD, SDLEN, SEMPTY, SMUTED = range(7)
            writes = [r for r in spk if not r[SEMPTY]]
            empties = [r for r in spk if r[SEMPTY]]
            f.write(f"\n{'='*90}\n")
            f.write(f"SPEAKER THREAD ({len(spk)} iterations, {len(writes)} writes, {len(empties)} empty waits)\n")
            f.write(f"{'='*90}\n")
            if writes:
                wait_ms = [r[SWAIT] for r in writes]
                write_ms = [r[SWR] for r in writes if r[SWR] >= 0]
                intervals = [spk[i+1][ST] - spk[i][ST] for i in range(len(spk)-1)
                             if not spk[i][SEMPTY] and not spk[i+1][SEMPTY]] if len(spk) > 1 else [0.05]
                f.write(f"\n  WRITE TIMING\n")
                if write_ms:
                    f.write(f"    stream.write(): mean={statistics.mean(write_ms):.1f}ms  "
                            f"min={min(write_ms):.1f}ms  max={max(write_ms):.1f}ms\n")
                f.write(f"    queue.get() wait: mean={statistics.mean(wait_ms):.1f}ms  "
                        f"max={max(wait_ms):.1f}ms\n")
                if intervals:
                    int_ms = [i * 1000 for i in intervals]
                    f.write(f"    write interval: mean={statistics.mean(int_ms):.1f}ms  "
                            f"stdev={statistics.stdev(int_ms):.1f}ms  max={max(int_ms):.1f}ms\n")
                dlens = set(r[SDLEN] for r in writes)
                f.write(f"    data lengths: {sorted(dlens)}\n")
                # Gaps: consecutive empties
                spk_gaps = []
                g = 0
                for r in spk:
                    if r[SEMPTY]:
                        g += 1
                    else:
                        if g > 0:
                            spk_gaps.append(g)
                        g = 0
                if g > 0:
                    spk_gaps.append(g)
                if spk_gaps:
                    f.write(f"    empty gaps: {len(spk_gaps)} (max {max(spk_gaps)} consecutive empties = "
                            f"{max(spk_gaps) * 100:.0f}ms)\n")
                else:
                    f.write(f"    empty gaps: none\n")

                # Per-write detail (first 100 + anomalies)
                f.write(f"\n  {'idx':>5} {'t(s)':>7} {'wait':>6} {'write':>6} {'qd':>3} {'len':>5} {'notes':>10}\n")
                f.write(f"  {'-'*50}\n")
                for i, r in enumerate(writes):
                    is_early = i < 100
                    is_anomaly = (r[SWR] > 80 or r[SWAIT] > 80 or r[SQD] >= 7 or r[SWR] < 0)
                    if is_early or is_anomaly:
                        notes = ''
                        if r[SWR] < 0:
                            notes = 'ERR'
                        elif r[SWR] > 60:
                            notes = 'SLOW'
                        elif r[SQD] >= 7:
                            notes = 'FULL'
                        flag = '*' if is_anomaly and not is_early else ' '
                        f.write(f"  {i:>4}{flag} {r[ST]:7.3f} {r[SWAIT]:6.1f} {r[SWR]:6.1f} "
                                f"{r[SQD]:3} {r[SDLEN]:5} {notes:>10}\n")

        f.write(f"\n{'='*90}\n")
        f.write(f"End of trace ({len(trace)} main ticks, {len(spk) if spk else 0} speaker iterations)\n")

    print(f"\n  Audio trace written to: {out_path}")
