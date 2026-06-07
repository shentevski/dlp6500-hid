"""
Minimal demo: generate a circle and show it on the DMD in pattern-on-the-fly mode.

Run from the repo root (the folder that contains the `dlpc900_hid` package):

    python examples/circle_otf.py

Requires: hidapi, pillow, numpy   (pip install hidapi pillow numpy)
The DMD must be connected over USB and powered. On Windows no driver install
is needed -- it works through the built-in HID driver.
"""
import sys
import os
import time

# Allow running the script directly from the repo root without installing.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlpc900_hid import DMD, patterns, list_hid_devices


def main():
    # 0. Show what HID devices are visible (helps confirm the DMD is detected).
    print("HID devices found:")
    for d in list_hid_devices():
        if d.get("vendor_id") == 0x0451:
            print(f"  -> TI device: VID={d['vendor_id']:#06x} "
                  f"PID={d['product_id']:#06x}  {d.get('product_string')}")

    # 1. Connect.
    dmd = DMD()
    try:
        model, firmware = dmd.get_hardware()
        print(f"\nConnected to {model}  (firmware: {firmware})")
        print("Hardware status:")
        print(dmd.get_hardware_status_for_humans())

        # 2. Figure out the native resolution and build a circle that size.
        try:
            width, height = dmd.get_resolution()
        except Exception:
            width, height = patterns.RESOLUTIONS.get(model, (1920, 1080))
        print(f"Using resolution {width} x {height}")

        circle = patterns.circle(width, height, radius=min(width, height) // 4)

        # 3. Upload + display in on-the-fly mode (looped indefinitely).
        print("Uploading circle and starting on-the-fly display...")
        dmd.show_image_otf(circle, exposure_us=1_000_000, dark_us=0)

        print("Circle is now displayed. Leaving it up for 10 s...")
        time.sleep(10)
        dmd.stop_pattern()
        print("Stopped.")
    finally:
        dmd.standby()
        dmd.close()


if __name__ == "__main__":
    main()
