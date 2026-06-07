"""
Low-level HID diagnostic for the DLP6500 / DLPC900.

Run this on the Windows PC with the DMD connected (and the TI GUI closed):

    python examples/diagnose.py

It enumerates the HID interfaces, sends the "get hardware" query (command
0x0206) with a few different framings, and prints the RAW bytes that come back.
Copy the whole output back so the read/write framing can be fixed exactly.
"""
import time

import hid

VID, PID = 0x0451, 0xC900


def show_enumeration():
    print("=== HID interfaces for VID 0x0451 ===")
    found = [d for d in hid.enumerate() if d.get("vendor_id") == VID]
    if not found:
        print("  NONE FOUND. Check USB cable/power and that no other app "
              "(e.g. the TI LightCrafter GUI) holds the device.")
        return None
    for d in found:
        print(f"  pid={d.get('product_id'):#06x} "
              f"usage_page={d.get('usage_page'):#06x} usage={d.get('usage'):#06x} "
              f"iface={d.get('interface_number')} "
              f"path={d.get('path')} "
              f"product={d.get('product_string')!r}")
    return found


def build_get_hardware_64():
    """flag=0xC0(read), seq=0x0A, length=2, command=0x0206 (little-endian)."""
    buf = [0xC0, 0x0A, 0x02, 0x00, 0x06, 0x02]
    buf += [0x00] * (64 - len(buf))
    return buf


def main():
    if show_enumeration() is None:
        return

    print("\n=== Opening device ===")
    h = hid.device()
    h.open(VID, PID)
    for label, fn in (("manufacturer", h.get_manufacturer_string),
                      ("product", h.get_product_string)):
        try:
            print(f"  {label}: {fn()!r}")
        except Exception as e:
            print(f"  {label}: <error {e!r}>")
    try:
        h.set_nonblocking(0)
    except Exception as e:
        print(f"  set_nonblocking error: {e!r}")

    cmd64 = build_get_hardware_64()

    # Try both framings: with a leading report-id byte (65B) and without (64B).
    for label, report in (("WRITE with report-id 0  (65 bytes)", [0x00] + cmd64),
                          ("WRITE without report-id  (64 bytes)", cmd64)):
        print(f"\n=== {label} ===")
        try:
            n = h.write(bytes(report))
            print(f"  write() returned {n}")
        except Exception as e:
            print(f"  WRITE ERROR: {e!r}")
            continue

        time.sleep(0.05)
        for rd_len in (65, 64):
            try:
                data = list(h.read(rd_len, 2000))
                print(f"  read({rd_len}) -> len={len(data)}  bytes={data[:20]}"
                      f"{' ...' if len(data) > 20 else ''}")
            except Exception as e:
                print(f"  read({rd_len}) ERROR: {e!r}")

    h.close()
    print("\nDone. Copy everything above.")


if __name__ == "__main__":
    main()
