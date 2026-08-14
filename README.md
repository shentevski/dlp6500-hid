# dlpc900_hid

A small Python wrapper to control a **DLPC900-based DMD** (Texas Instruments /
Thorlabs LightCrafter — DLP6500, DLP9000, DLP670S, DLP500YX, DLP5500) over USB
using the **`hid` package**.

The key difference from the upstream
[`dlpyc900`](https://github.com/WetenSchaap/dlpyc900) project: that one uses
`pyusb`, which on Windows forces you to replace the device driver with libusb
via **Zadig**. This wrapper instead talks to the DMD through the **`hid`
package**, which uses Windows' **built-in HID driver** — so **no driver
installation is required**, and the TI GUI keeps working when you unplug/replug.

> 📖 **New here? Read [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md)** — a
> first-principles, pedagogical tour of USB-HID transport, the DLPC900 command
> format, pattern-on-the-fly display, RLE compression, generating DMD-safe
> patterns, and the debugging story behind it all.

## Install

Install the package straight from GitHub (this pulls in `hidapi`, `pillow`, and
`numpy` automatically):

```bash
pip install git+https://github.com/shentevski/dlp6500-hid.git
```

This is a **private** repo, so on the Windows PC you need to be authenticated to
GitHub. Easiest options:

```bash
# Option A: GitHub CLI (recommended) -- installs gh, logs in, then pip can use it
winget install GitHub.cli
gh auth login                       # choose HTTPS + "yes, use as git credential helper"
pip install git+https://github.com/shentevski/dlp6500-hid.git

# Option B: personal access token (PAT) inline
pip install git+https://<YOUR_TOKEN>@github.com/shentevski/dlp6500-hid.git
```

### Editable / multi-machine workflow (recommended for development)

Clone once and install in **editable** mode. After that, code changes only need
`git pull` — the installed package points at the cloned source, so there's no
reinstall for `.py` edits.

```bash
# one-time, on every machine (dev box and lab PC):
gh auth login                                          # private repo needs auth
git clone https://github.com/shentevski/dlp6500-hid.git
cd dlp6500-hid
pip install -e .                                       # editable install + deps

# day to day:
#   on the dev machine:   git add -A && git commit -m "..." && git push
#   on the other machine: git pull          <- changes are live immediately
```

Only re-run `pip install -e .` if you change **dependencies** in
`pyproject.toml` (editable mode already covers all Python source edits).

> **Distribution name is `dlp6500-hid`, but you `import dlpc900_hid`** (like
> `pip install pillow` → `import PIL`).

`hidapi` ships prebuilt wheels for Windows (and bundles the native library), so
there is nothing else to install. The DLPC900 enumerates as a standard HID
device (VID `0x0451`, PID `0xc900`) that Windows drives natively.

> Only requirement: nothing else may hold the device open at the same time —
> close the TI **DLP LightCrafter GUI** before running Python.

## Quick start — circle on-the-fly

```python
from dlpc900_hid import DMD, patterns

with DMD() as dmd:                       # opens the HID connection
    print(dmd.get_hardware())            # e.g. ('DLP6500', 'firmware tag')
    w, h = dmd.get_resolution()          # native mirror-array size
    circle = patterns.circle(w, h, radius=min(w, h)//4)
    dmd.show_image_otf(circle, exposure_us=1_000_000)   # generate + upload + display
    input("Circle displayed. Press Enter to stop...")
# leaving the `with` block puts the DMD in standby and closes the handle
```

Or just run the bundled demo:

```bash
python examples/circle_otf.py
```

## Experiment pattern sets

`patterns` also has two classes that generate ready-to-display binary images at
the DMD's native resolution:

```python
from dlpc900_hid import DMD, patterns

mw = patterns.MixWavelengths()   # solid_on/off, one_line, two_lines, three_lines
hb = patterns.HBBrush()          # circle, ring

mw.one_line(offset=0, width=20, orientation="45")   # one 45° stripe through center
mw.three_lines(offsets=[-150, 0, 150], widths=[20, 20, 20], orientation="45")
hb.ring(center=(960, 540), radius=300, width=40)
```

- **Lines** take an `orientation` (`"vertical"`, `"horizontal"`, `"45"`, or a
  number in degrees) and either an absolute `position`/`positions` or a
  center-relative `offset`/`offsets` (pixels; `0` = centered). On the DLP6500 a
  *lab-horizontal* stripe is a **45° line in pixel space** (the mirror array is
  rotated 45°) — see the doc.
- `on=True` draws a bright shape on a dark field; `on=False` inverts it.
- **Exact mirror-row lines** — `one_line_rows` / `two_lines_rows` /
  `three_lines_rows` size `width`/`offset` in **mirror rows** (not pixels) and
  deliver *exactly* `width` rows at every offset. Use these for quantitative
  **diagonal** scans: the pixel-distance methods hold `√2·width` rows at ±45°,
  which isn't an integer, so their row count (and delivered area) flips as you
  sweep. Restricted to 0/90/±45°. See [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) §4.

## Fast multi-pattern switching

```python
dmd.load_patterns([mw.solid_on(), hb.circle(radius=270), mw.one_line(960, 20)])
dmd.display_pattern(1)    # show the circle; switching is ~milliseconds
```
`load_patterns` encodes each image once; `display_pattern(i)` re-streams image
`i` and restarts without the slow mode change.

## Compression

`dmd.compression` selects the pattern encoding (also per-call via `compression=`):

| value | type | notes |
|-------|------|-------|
| `'erle'` | enhanced RLE | **default** — smallest, fastest upload |
| `'rle'`  | basic RLE | fallback if a unit mis-decodes enhanced |
| `'none'` | uncompressed | reliable but ~6 MB/upload (slow) |

> **Important hardware note:** pattern *data-load* packets must be sent without
> requesting an ACK, or the DMD's on-the-fly RLE decompression is corrupted (the
> library does this for you). This was the root cause of a long "patterns don't
> show" saga — see [`docs/HOW_IT_WORKS.md`](docs/HOW_IT_WORKS.md) §5.

## What `show_image_otf` does

It runs the full TI "Pattern On-The-Fly" sequence for you:

1. `stop_pattern()` — stop any running sequence
2. `set_display_mode("otf")` — enter on-the-fly mode
3. `setup_pattern_LUT_definition(...)` — define a 1-entry LUT (exposure, color, bit depth)
4. `configure_pattern_from_LUT(1, 0)` — show that 1 entry, loop forever
5. `upload_image(0, image)` — compress and stream the image over USB
6. `start_pattern()` — display it

You can call those steps individually if you want multiple patterns, triggering,
custom exposure/dark times, etc.

## Package layout

| File | Purpose |
|------|---------|
| `dlpc900_hid/dlpc900.py` | `DMD` class — HID transport + DLPC900 command protocol |
| `dlpc900_hid/erle.py`    | Enhanced-RLE image compression (the format the DMD wants) |
| `dlpc900_hid/patterns.py`| `circle()`, `ring()`, `solid()` image generators |
| `dlpc900_hid/errors.py`  | `DMDError` |
| `examples/circle_otf.py` | end-to-end demo |

## Troubleshooting

- **`DMDError: Could not open DMD`** — device not plugged in/powered, or the TI
  GUI (or another script) is holding it open. Close other programs and retry.
- **Confirm the DMD is seen:** `python -c "from dlpc900_hid import list_hid_devices; [print(d) for d in list_hid_devices() if d['vendor_id']==0x0451]"`
- **On macOS/Linux** the same code works, but you may need permissions
  (`sudo`, or a udev rule on Linux granting access to VID `0451`).
- **Image looks wrong / nothing shows in OTF mode** — TI's on-the-fly mode is a
  bit finicky. Make sure the image matches the DMD's native resolution and try a
  longer `exposure_us`. `patterns.solid(w, h, 255)` (all mirrors on) is a good
  sanity check.

## A note on your hardware

The PDF in this folder (`dlpz005a.pdf`) is the datasheet for the **DLP3000 /
DLPC300** (LightCrafter 3000) — an **older controller with a completely
different USB/I²C protocol**. This wrapper targets the **DLPC900** family
(LightCrafter 6500/9000 and Thorlabs equivalents), which is what `dlpyc900`,
"pattern on-the-fly", and the HID approach all imply. If your device is actually
a LightCrafter 3000, this code will not talk to it and we'd need a different
protocol — let me know.

## Credits & license

Command protocol and the ERLE encoder are adapted from
[`dlpyc900`](https://github.com/WetenSchaap/dlpyc900) by Piet J.M. Swinkels
(itself derived from [Pycrafter6500](https://github.com/csi-dcsc/Pycrafter6500)),
which is **GPLv3**. This wrapper is therefore distributed under the same GPLv3
terms. Reference: TI DLPC900 Programmer's Guide, DLPU018.
