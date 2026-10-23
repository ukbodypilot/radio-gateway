"""Broadcastify/Icecast stream statistics.

Extracted from gateway_core.py.
"""



def get_stream_stats(gw):
    """Get live streaming statistics from direct Icecast connection."""
    so = getattr(gw, 'stream_output', None)
    if not so:
        return {}
    # Error info is returned even while DISCONNECTED — that is precisely when
    # it matters. Returning a bare {} here used to leave the dashboard showing
    # "Stopped" with no indication of why.
    _err = {
        'last_error': getattr(so, '_last_error', '') or '',
        'last_error_time': getattr(so, '_last_error_time', 0) or 0,
    }
    if not so.connected:
        return dict(_err, connected=False)
    uptime_s = int(so.uptime)
    # NOTE: rg_stream_bytes_sent_total is NOT updated here any more. This
    # function only runs when a web request asks for gateway status, so
    # feeding a Prometheus counter from it meant the counter advanced only
    # when somebody had the dashboard open — flat the rest of the time, then
    # one huge step. rate() read 0 across every gap, so the
    # 'broadcastify_stream_down' alert emailed 4-11 times a day about a
    # healthy stream. The counter is now incremented in
    # StreamOutputSource._reader (sources/stream_output.py), where the bytes are
    # actually written to the socket. Don't reintroduce it here.
    return {
        **_err,
        'connected': True,
        'uptime': uptime_s,
        'bytes_sent': int(so._bytes_sent),
        'send_rate': f"{so._bytes_sent * 8 / max(uptime_s, 1) / 1000:.1f} kbps" if uptime_s > 0 else '—',
        'server': getattr(gw.config, 'STREAM_SERVER', ''),
        'mount': getattr(gw.config, 'STREAM_MOUNT', ''),
        'bitrate': int(getattr(gw.config, 'STREAM_BITRATE', 16)),
        'sample_rate': int(getattr(gw.config, 'STREAM_SAMPLE_RATE', 22050)),
        'dual_channel': bool(getattr(gw.config, 'STREAM_DUAL_CHANNEL', True)),
    }
