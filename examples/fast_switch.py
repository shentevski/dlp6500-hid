"""
Fast pattern switching: upload images ONCE, then toggle between them instantly.

The slow part of on-the-fly display is streaming the image over USB. So upload
every pattern up front into its own memory slot, then switching is just a few
tiny commands (re-point the LUT + restart) -- milliseconds, not ~1 s.

    python examples/fast_switch.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlpc900_hid import DMD, patterns

W, H = 1920, 1080

with DMD() as dmd:
    print("connected:", dmd.get_hardware())

    # --- one-time setup (the slow part) ---
    dmd.enter_otf_mode()                       # switch to OTF once (~0.5 s)
    # upload HIGHEST index first:
    dmd.upload_pattern(1, patterns.circle(W, H, radius=270))
    dmd.upload_pattern(0, patterns.solid(W, H, 255))
    print("patterns uploaded.")

    # --- fast toggling (each switch is ~instant) ---
    current = 0
    names = {0: "solid", 1: "circle"}
    while True:
        dmd.display_pattern(current)           # no re-upload -> fast
        cmd = input(f"showing '{names[current]}'. Enter=toggle, q=quit: ")
        if cmd.strip().lower() == "q":
            break
        current ^= 1                           # flip 0<->1

    dmd.stop_pattern()
    print("done.")
