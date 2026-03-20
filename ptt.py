#!/usr/bin/env python3
"""PTT / relay controller classes for radio-gateway."""

import sys
import os
import time
import signal
import threading
import threading as _thr
import subprocess
import shutil
import json as json_mod
import collections
import queue as _queue_mod
from struct import Struct
import socket
import select
import array as _array_mod
import math as _math_mod
import re
import numpy as np

class RelayController:
    """Controls a CH340 USB relay module via serial (4-byte commands)."""

    CMD_ON  = bytes([0xA0, 0x01, 0x01, 0xA2])
    CMD_OFF = bytes([0xA0, 0x01, 0x00, 0xA1])

    def __init__(self, device, baud=9600):
        self._device = device
        self._baud = baud
        self._port = None
        self._state = None  # None=unknown, True=on, False=off

    def open(self):
        try:
            import serial
            self._port = serial.Serial(self._device, self._baud, timeout=1)
            return True
        except Exception as e:
            print(f"  [Relay] Failed to open {self._device}: {e}")
            return False

    def close(self):
        if self._port:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None

    def set_state(self, on):
        """Set relay on (True) or off (False). Returns True on success."""
        if not self._port:
            return False
        try:
            self._port.write(self.CMD_ON if on else self.CMD_OFF)
            self._state = on
            return True
        except Exception as e:
            print(f"  [Relay] Write error on {self._device}: {e}")
            return False

    @property
    def state(self):
        return self._state


class GPIORelayController:
    """Controls a relay via Raspberry Pi GPIO pin (BCM numbering)."""

    def __init__(self, gpio_pin):
        self._pin = gpio_pin
        self._state = None
        self._gpio = None

    def open(self):
        try:
            import RPi.GPIO as GPIO
            self._gpio = GPIO
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._pin, GPIO.OUT, initial=GPIO.LOW)
            self._state = False
            return True
        except Exception as e:
            print(f"  [GPIORelay] Failed to setup GPIO {self._pin}: {e}")
            return False

    def close(self):
        if self._gpio:
            try:
                self._gpio.cleanup(self._pin)
            except Exception:
                pass

    def set_state(self, on):
        """Set relay on (True) or off (False). Returns True on success."""
        if not self._gpio:
            return False
        try:
            self._gpio.output(self._pin, self._gpio.HIGH if on else self._gpio.LOW)
            self._state = on
            return True
        except Exception as e:
            print(f"  [GPIORelay] Error setting GPIO {self._pin}: {e}")
            return False

    @property
    def state(self):
        return self._state


