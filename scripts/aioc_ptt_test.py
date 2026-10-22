#!/usr/bin/env python3
"""
AIOC PTT Control Script
Press 'T' to key PTT, any other key to unkey
"""

import sys
import os
from struct import Struct

try:
    import hid
except ImportError:
    print("ERROR: hidapi library not found!")
    print("Install it with: pip3 install hidapi --break-system-packages")
    sys.exit(1)

try:
    import pygame
    pygame.mixer.init()
    SOUND_AVAILABLE = True
except ImportError:
    print("WARNING: pygame not found - sound playback disabled")
    SOUND_AVAILABLE = False
except Exception as e:
    print(f"WARNING: Could not initialize pygame: {e}")
    SOUND_AVAILABLE = False

AIOC_VID = 0x1209
AIOC_PID = 0x7388

class PTTChannel:
    PTT1 = 3
    PTT2 = 4

def set_ptt_state_raw(device, pin_num, state_on):
    """Set PTT state using raw HID write"""
    state = 1 if state_on else 0
    iomask = 1 << (pin_num - 1)
    iodata = state << (pin_num - 1)
    data = Struct("<BBBBB").pack(0, 0, iodata, iomask, 0)
    device.write(bytes(data))

def load_sound(sound_file=None):
    """Load sound file"""
    if not SOUND_AVAILABLE:
        return None
    
    # Try to load sound file
    if sound_file and os.path.exists(sound_file):
        try:
            print(f"Loading sound: {sound_file}")
            return pygame.mixer.Sound(sound_file)
        except Exception as e:
            print(f"Could not load {sound_file}: {e}")
    
    # Try common filenames
    for filename in ['beep.mp3', 'beep.wav', 'ptt_beep.mp3', 'ptt_beep.wav']:
        if os.path.exists(filename):
            try:
                print(f"Found and loading: {filename}")
                return pygame.mixer.Sound(filename)
            except Exception as e:
                print(f"Could not load {filename}: {e}")
    
    print("No sound file found, will run without sound")
    return None

def main():
    print("=" * 60)
    print("AIOC PTT Control")
    print("=" * 60)
    print()
    
    # Open AIOC device
    try:
        print(f"Opening AIOC device (VID: 0x{AIOC_VID:04x}, PID: 0x{AIOC_PID:04x})...")
        aioc_device = hid.Device(vid=AIOC_VID, pid=AIOC_PID)
        print("✓ Device opened successfully")
        print()
        
        print(f"Manufacturer: {aioc_device.manufacturer}")
        print(f"Product: {aioc_device.product}")
        print(f"Serial No: {aioc_device.serial}")
        print()
        
    except (OSError, hid.HIDException) as e:
        print(f"ERROR: Could not open AIOC device: {e}")
        print()
        print("Troubleshooting:")
        print("1. Check USB connection: lsusb | grep 1209:7388")
        print("2. Try running with sudo: sudo python3 aioc_ptt_test.py")
        sys.exit(1)
    
    # Load sound
    sound_obj = load_sound()
    if sound_obj:
        print("✓ Sound loaded")
    else:
        print("✗ Running without sound")
    print()
    
    print("=" * 60)
    print("CONTROLS:")
    print("  Press 'T' key     = PTT ON")
    print("  Press SPACE       = PTT OFF")
    print("  Press 'Q'         = Exit")
    print("=" * 60)
    print()
    print("Ready! Press 'T' to transmit, SPACE to stop...")
    print()
    
    ptt_active = False
    
    # Disable echo and line buffering
    import termios
    import tty
    
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    
    try:
        tty.setcbreak(fd)  # Use cbreak mode instead of raw
        
        while True:
            # Read one character (blocking)
            ch = sys.stdin.read(1)
            
            # Check for quit
            if ch.lower() == 'q':
                print("\nExiting...")
                break
            
            # Check for 't' or 'T' - turn PTT ON
            elif ch.lower() == 't':
                if not ptt_active:
                    sys.stdout.write("\rPTT ON  ")
                    sys.stdout.flush()
                    set_ptt_state_raw(aioc_device, PTTChannel.PTT1, True)
                    ptt_active = True
                    
                    # Play sound in loop
                    if sound_obj:
                        sound_obj.play(-1)
            
            # Space or any other key - turn PTT OFF
            else:
                if ptt_active:
                    sys.stdout.write("\rPTT OFF ")
                    sys.stdout.flush()
                    set_ptt_state_raw(aioc_device, PTTChannel.PTT1, False)
                    ptt_active = False
                    
                    # Stop sound
                    if sound_obj:
                        sound_obj.stop()
            
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    finally:
        # Restore terminal settings
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        
        # Cleanup
        if ptt_active:
            print("\nEnsuring PTT is OFF...")
            set_ptt_state_raw(aioc_device, PTTChannel.PTT1, False)
            if sound_obj:
                sound_obj.stop()
        
        aioc_device.close()
        print("Closed AIOC device")

if __name__ == "__main__":
    main()
