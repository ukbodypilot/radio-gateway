"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""

import collections
import json as json_mod
import math as _math_mod
import os
import queue as _queue_mod
import re
import socket
import struct
import subprocess
import sys
import threading
import time


class _StreamMixin:
    def _get_stream_stats(self):
        from stream_stats import get_stream_stats
        return get_stream_stats(self)

    def _send_stream_alert(self, message, subject=None):
        """Send Broadcastify stream alert via email.

        `subject` is the email subject line, minus the hostname suffix. It
        used to be hardcoded to "Broadcastify Stream Down" for EVERY alert,
        including recovery ones, so the five recovery mails sent during the
        2026-08-21 stall all arrived announcing an outage — the body said
        "recovered" and the subject said "Down", and the subject is the half
        you see in a notification. Callers now say which event this is.

        The default is deliberately neutral rather than "Down": a future
        caller that forgets to pass one gets a vague subject, not a false
        one. That is exactly how the original bug read.

        Fire-and-forget: the caller is status_monitor_loop — the thread
        that enforces the legacy PTT release timeout and runs the
        watchdogs. Synchronous SMTP (15s timeout across several socket
        ops) here used to block PTT unkey for up to a
        minute, exactly during network/DNS outages when streams drop.
        """
        threading.Thread(
            target=self._send_stream_alert_blocking, args=(message, subject),
            daemon=True, name='StreamAlert',
        ).start()

    def _send_stream_alert_blocking(self, message, subject=None):
        import datetime
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # Email alert
        if self.email_notifier:
            try:
                import socket
                hostname = socket.gethostname()
            except Exception:
                hostname = ''
            line = subject or 'Broadcastify Stream Alert'
            subject = f"{line}{' — ' + hostname if hostname else ''}"
            body = f"{message}\n\nTime: {now}\n\n-- Radio Gateway"
            self.email_notifier.send(subject, body)

