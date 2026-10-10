#!/usr/bin/env python3
"""Load the factory TAS2563 speaker calibration from coreboot VPD into the driver.

Googlebook OS applied these per-speaker values (R0, R0 low, 1/R0, power,
temperature limit) from userspace; the tas2781 driver otherwise protects the
speakers with uncalibrated defaults. VPD keys dsm_calib_register_array and
dsm_calib_value_0..3 are written at the factory and live in firmware flash.
The control is write-only (reads return EPERM), so amixer cannot set it.
"""
import ctypes
from pathlib import Path
import sys
import time

CARD = 'hw:sofrt5682'
CONTROL = b'Speaker Calibrated Data'
VPD = Path('/sys/firmware/vpd/ro')
AMPS = 4
SND_CTL_ELEM_IFACE_MIXER = 2


def payload():
    regs = bytes.fromhex((VPD / 'dsm_calib_register_array').read_text().strip())
    vals = [bytes.fromhex((VPD / f'dsm_calib_value_{i}').read_text().strip()) for i in range(AMPS)]
    if len(regs) != 16 or regs[0] != ord('r'):
        raise SystemExit('Unexpected dsm_calib_register_array format')
    if any(len(v) != 21 or v[0] != i for i, v in enumerate(vals)):
        raise SystemExit('Unexpected dsm_calib_value format')
    data = bytes([1 + len(regs) + 21 * AMPS]) + regs + b''.join(vals)
    for i, v in enumerate(vals):
        r0 = int.from_bytes(v[1:5], 'big') / 2**27
        print(f'amp {i}: R0 {r0:.2f} ohm')
    return data


def write(data):
    asound = ctypes.CDLL('libasound.so.2')
    ctl, elem_id, value = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if asound.snd_ctl_open(ctypes.byref(ctl), CARD.encode(), 0) < 0:
        return False
    try:
        asound.snd_ctl_elem_id_malloc(ctypes.byref(elem_id))
        asound.snd_ctl_elem_value_malloc(ctypes.byref(value))
        asound.snd_ctl_elem_id_set_interface(elem_id, SND_CTL_ELEM_IFACE_MIXER)
        asound.snd_ctl_elem_id_set_name(elem_id, CONTROL)
        asound.snd_ctl_elem_value_set_id(value, elem_id)
        for i, b in enumerate(data):
            asound.snd_ctl_elem_value_set_byte(value, ctypes.c_uint(i), ctypes.c_ubyte(b))
        rc = asound.snd_ctl_elem_write(ctl, value)
        if rc == -2:  # ENOENT: amplifier firmware not loaded yet
            return False
        if rc < 0:
            raise SystemExit(f'snd_ctl_elem_write failed: {rc}')
        return True
    finally:
        asound.snd_ctl_close(ctl)


def main():
    data = payload()
    # The control appears once SOF and the amplifier firmware have loaded.
    for _ in range(60):
        try:
            if write(data):
                print('Factory speaker calibration loaded')
                return
        except OSError:
            pass
        time.sleep(1)
    sys.exit('Sound card or calibration control did not appear')


if __name__ == '__main__':
    main()
