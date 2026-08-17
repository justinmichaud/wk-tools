#!/usr/bin/env python3
# Create a virtual (fake) input device via /dev/uinput so weston exposes a
# seat_default. Without this, cog aborts at startup with:
#   cog_wl_platform_create_im_context: assertion failed: (display->seat_default)
# Run in the background and leave it running for the whole session:
#   setsid python3 fakeseat.py >/tmp/fakeseat.log 2>&1 &
# Clean up with:  pkill -f fakeseat.py    (or: pkill -f signal.pause)
import fcntl, os, struct, signal

def IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (typ << 8) | nr

U = ord('U')  # uinput ioctl type
fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
fcntl.ioctl(fd, IOC(1, U, 100, 4), 1)   # UI_SET_EVBIT, EV_KEY
fcntl.ioctl(fd, IOC(1, U, 101, 4), 1)   # UI_SET_EVBIT, (second class)
# struct uinput_user_dev: name[80], id (4x u16), ff_effects_max (u32), 256 abs ints
os.write(fd, struct.pack('=80s4HI256i', b'cog-fake-seat', 3, 1, 1, 1, 0, *([0] * 256)))
fcntl.ioctl(fd, IOC(0, U, 1, 0), 0)     # UI_DEV_CREATE
signal.pause()
