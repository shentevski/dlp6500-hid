"""
dlpc900_hid -- control a DLPC900-based DMD over USB-HID (no Zadig / libusb).

Quick start::

    from dlpc900_hid import DMD, patterns

    with DMD() as dmd:
        w, h = dmd.get_resolution()
        circle = patterns.circle(w, h, radius=300)
        dmd.show_image_otf(circle, exposure_us=1_000_000)
"""
from .dlpc900 import (
    DMD, dmd, VENDOR_ID, PRODUCT_ID, HARDWARE_CODES,
    list_hid_devices, parse_reply,
)
from .errors import DMDError, DMDerror
from . import patterns
from . import erle

__all__ = [
    "DMD", "dmd", "DMDError", "DMDerror", "patterns", "erle",
    "VENDOR_ID", "PRODUCT_ID", "HARDWARE_CODES",
    "list_hid_devices", "parse_reply",
]
