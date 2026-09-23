#!/usr/bin/env python3
# A /dev/uinput device, so weston exposes a seat_default: without one cog aborts at startup on `cog_wl_platform_create_im_context: assertion failed: (display->seat_default)`. Run under setsid for the whole session; `pkill -f fakeseat.py` ends it.
import fcntl, os, struct, signal

def IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (typ << 8) | nr

U = ord('U')
fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
fcntl.ioctl(fd, IOC(1, U, 100, 4), 1)
fcntl.ioctl(fd, IOC(1, U, 101, 4), 1)
os.write(fd, struct.pack('=80s4HI256i', b'cog-fake-seat', 3, 1, 1, 1, 0, *([0] * 256)))
fcntl.ioctl(fd, IOC(0, U, 1, 0), 0)
signal.pause()
