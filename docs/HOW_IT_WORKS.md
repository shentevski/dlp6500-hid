# How `dlpc900_hid` works — a guided tour

This document explains, from first principles, how this library drives a
**DLP6500 DMD** (on a single-DLPC900 LightCrafter board) over USB from Python —
and *why* each piece is the way it is. It doubles as a worked example of how to
reverse-engineer and debug a binary hardware protocol, because getting this to
work was a multi-layer detective story and the lessons are worth keeping.

If you read it top to bottom you should come away understanding: USB-HID
transport, the DLPC900 command format, pattern-on-the-fly display, RLE image
compression, how to generate patterns that a DMD can actually show, and a
repeatable method for debugging this kind of system.

---

## 0. The mental model

A DMD (Digital Micromirror Device) is a chip covered in ~2 million tiny mirrors,
each of which you can tilt to one of two angles: **"on"** (reflects light toward
your optics) or **"off"** (reflects it into a dump). A pattern is just "which
mirrors are on." The **DLPC900** is the controller chip that drives the mirror
array and talks to your PC over USB.

The data flow this library implements:

```
  your idea          PIL image          compressed bytes        USB-HID packets        DLPC900        mirrors
 "a 45° line"  ->  1920x1080 binary  ->  RLE 'Spld' blob   ->   64-byte reports   ->  decompress  ->  light
   patterns.py         patterns.py          erle.py             dlpc900.py            (hardware)
```

Package map:

| File | Responsibility |
|------|----------------|
| `dlpc900_hid/patterns.py` | Make pattern **images** (numpy → PIL): solids, lines, circles, rings |
| `dlpc900_hid/erle.py`     | **Encode** an image into the DLPC900's compressed `Spld` byte format |
| `dlpc900_hid/dlpc900.py`  | **Transport + protocol**: USB-HID, the command set, the display sequence |
| `dlpc900_hid/errors.py`   | `DMDError` |

Three layers, three questions to keep separate when something breaks: *is the
image right? are the bytes right? is the transport right?* (More on that in §5.)

---

## 1. Talking to the chip: USB-HID transport

### Why HID (and no driver install)

USB devices speak through a *driver*. The DLPC900 enumerates as a standard
**USB-HID** device (vendor `0x0451`, product `0xc900`) — the same class as a
keyboard or mouse — and every OS ships a built-in HID driver. So we can talk to
it through the `hid` Python package with **nothing to install**.

The popular alternative (`pyusb`) needs a *libusb* backend, which on Windows
means replacing the device's driver with Zadig/WinUSB — and then the TI GUI
stops working until you swap it back. Using HID avoids all of that. This was a
deliberate design choice and it's why the library is `hid`-based.

### HID reports: fixed 64-byte packets

HID doesn't send arbitrary byte streams; it sends fixed-size **reports**. The
DLPC900 uses **64-byte** reports in each direction. Two practical consequences:

- **Every write is 65 bytes**: a leading **report-ID byte** (`0x00`, because this
  device doesn't use numbered reports) followed by 64 payload bytes. That's why
  `_write_report` does `bytes([0x00]) + bytes(buffer)`.
- **Anything longer than one report must be split** across several 64-byte
  reports, and the device reassembles them.

### The DLPC900 command frame

Inside those 64 bytes, every command has this layout (from TI's
DLPC900 Programmer's Guide, DLPU018):

```
 byte 0     byte 1      bytes 2-3        bytes 4-5       bytes 6..
+--------+-----------+---------------+----------------+-------------------+
|  flag  | sequence  | length (LE)   | command (LE)   |   payload ...     |
+--------+-----------+---------------+----------------+-------------------+
```

- **flag** — bitfield (see below).
- **sequence** — an arbitrary tag you choose; the device echoes it in its reply
  so you can match replies to commands.
- **length** — little-endian count of the bytes that follow the length field
  (i.e. 2 command bytes + payload).
- **command** — the 16-bit opcode, little-endian (e.g. `0x1A1B` = set display mode).
- **payload** — command-specific data.

See `_build_reports()` in `dlpc900.py`.

### The flag byte — and the bug that cost us a week

The flag byte has two bits that matter:

- **bit 7 (`0x80`)** — read (1) vs write (0).
- **bit 6 (`0x40`)** — "reply/ack requested": tells the device to send a
  response packet back.

Reads obviously need the reply bit. Most writes set it too (so you can confirm
the command landed). **But there is one critical exception:**

> **The pattern *data-load* commands (`0x1A2B`/`0x1A2D`) must NOT request a reply.**

Why? The DLPC900 **decompresses the pattern image on the fly, as the bytes stream
in**. If you ask it for an ack in the middle of that stream, the ack-handling
collides with the decompression and **corrupts the pattern**. Uncompressed
uploads have no decompression step, so they're immune — which is exactly the
symptom that finally cracked the case (§5). TI's own GUI clears this bit for
data loads; we now do too (`pattern_bmp_load(..., request_reply=False)`).

### Reading replies: the HID buffering gotcha

Here's a subtlety that doesn't exist over pyusb. Because writes can request a
reply, the device queues a response in the OS's **HID input ring buffer**. Over
pyusb those unread responses get dropped; the Windows HID driver *keeps* them.
So if you do a few writes and then a read, your read returns a **stale** reply
from an earlier write, not the answer to your query.

Two defenses, both in `send_command`:
1. **Flush** the input buffer (non-blocking reads until empty) *before* issuing a
   read query (`_flush_input`).
2. **Validate the echoed sequence byte** of the reply and **retry** if it's empty
   or mismatched (the DLPC900 also occasionally returns an empty reply if polled
   too quickly).

Note: a `0` timeout on a HID read means "block forever" on the Windows backend
(not "poll") — so the flush uses a small *positive* timeout. That detail caused a
hang until we found it.

---

## 2. Showing a pattern: Pattern-On-The-Fly

The DLPC900 has several **display modes** (`0x1A1B`): video (HDMI/DP), pre-stored
patterns (from flash), video-pattern, and **on-the-fly (OTF)**. OTF streams
pattern images over USB into the controller's RAM and displays them — perfect for
generating arbitrary patterns from code without reflashing firmware.

### The choreography

Displaying one image is a fixed sequence of commands (`show_image_otf`):

```
 1. stop_pattern()              0x1A24 = 0   stop any running sequence
 2. set_display_mode("otf")     0x1A1B = 3   enter on-the-fly mode
 3. setup_pattern_LUT_definition 0x1A34      define one LUT entry (see below)
 4. configure_pattern_from_LUT  0x1A31       "play 1 entry, loop forever"
 5. upload_image(0, img)        0x1A2A/2B    stream the compressed image
 6. start_pattern()             0x1A24 = 2   start displaying
```

Order matters: you must be stopped to change the mode/LUT, and you must define
what to show before you start. (Notably we do *not* point the input source at
flash — that would show the factory patterns instead of ours.)

### The LUT entry — what each knob does

A "LUT entry" (`0x1A34`) describes one pattern in the sequence:

- **exposure / dark time** (µs) — how long the pattern is lit / dark each cycle.
- **bit depth** (1–8) — grayscale depth. For pure on/off patterns, the value
  255 lights a mirror at any bit depth; 1-bit is fastest.
- **color** (1=R, 2=G, 4=B, 7=all) — which LED(s) illuminate.
- **image index** — which uploaded image this entry displays.
- **trigger** — internal/free-run vs external. The options byte packing here is
  fiddly; we matched it to the validated Pycrafter6500 layout.

### Fast switching

The slow part of OTF is streaming the image. `load_patterns([...])` pre-encodes a
list of images once; `display_pattern(i)` then re-streams image *i* and restarts
**without** re-entering OTF mode (the ~0.5 s mode change was the real bottleneck,
not the ~10 KB upload). We re-stream rather than "switch LUT index" because this
firmware doesn't reliably hold multiple resident OTF images for index switching.

---

## 3. Encoding the image: RLE compression

### Why compress at all

A full-frame image is `1920 × 1080 × 3 bytes ≈ 6.2 MB`. HID moves 64 bytes per
report, so an uncompressed upload is ~98,000 USB transactions ≈ **a minute**.
A typical binary pattern compresses to **~5–15 KB** → a fraction of a second.
Compression isn't an optimization here; it's the difference between usable and
not.

### The 48-byte `Spld` header

Every pattern image is prefixed with a 48-byte header (TI's `SPL_Header_t`):

```
 0  "Spld"  signature (0x53 0x70 0x6C 0x64)
 4  uint16  width
 6  uint16  height
 8  uint32  number of compressed bytes that follow
12  uint32  subimage offset  = 0xFFFFFFFF (none)
16  uint32  subimage end     = 0xFFFFFFFF (none)
20  uint32  background color  = 0x00RRGGBB (we use 0)
24  uint8   pixel format = 1  (24-bit packed RGB)   <- must be 1, not 0!
25  uint8   compression  = 0 none / 1 RLE / 2 enhanced RLE
26  uint8   byte order   = 1
29  uint8   IsLeftImage  = 1
... rest zero (padded to 48)
```

Two header fields bit us: `pixel format` must be **1** (we had `0`, which the
spec lists as "not supported"), and `IsLeftImage` should be **1**.

### Run-length encoding, DLPC900 style

RLE replaces runs of identical pixels with `(count, pixel)`. The DLPC900 control
bytes (DLPU018 §2.4.3.2):

| Control 1 | Control 2 | Meaning |
|-----------|-----------|---------|
| `n > 1`   | —         | repeat the next pixel `n` times |
| `0`       | `n > 1`   | `n` uncompressed (literal) pixels follow |
| `0`       | `1`, then `n` | copy `n` pixels from the **previous line** |
| `0`       | `1`, then `0` | end of image |
| `0`       | `0`       | end of line |

Counts < 128 are one byte; ≥ 128 use two bytes: `(n & 0x7F) | 0x80`, then `n >> 7`.

- **Basic RLE** (type 1): repeat + literal runs only, counts capped at 255, with
  per-line end markers and 4-byte alignment.
- **Enhanced RLE** (type 2): adds the **copy-from-previous-line** command, which
  is huge for patterns with vertical similarity (a vertical line compresses to a
  few KB instead of ~16 KB). This is the default (`dmd.compression = 'erle'`).

Our `enhanced_rle_encode` and `basic_rle_encode` are faithful ports of TI's
`compress.c` (`RLE_CompressBMPSpl` / `RLE_CompressBMP`) — we verified them
**byte-for-byte identical** to TI's output by compiling TI's C and diffing.
(Pixel bytes go out in a `[R,B,G]` order; irrelevant for grayscale patterns
where R=G=B.)

---

## 4. Generating patterns a DMD can actually show

The image side has its own rules, separate from the protocol.

### Rule 1: native resolution, one pixel per mirror

Build at exactly `1920 × 1080` so each image pixel maps to one mirror. Don't
generate at another size and let something scale it.

### Rule 2: strictly binary — no anti-aliasing

A DMD mirror is on or off; there is no "gray." If your drawing has smooth
(anti-aliased) edges, those gray edge pixels get thresholded (1-bit) or turned
into flickering time-dithered mirrors (8-bit) — either way your crisp slit or
ring is ruined. So we **never** use a drawing library that smooths; we build a
**boolean mask** (each pixel exactly True/False) and map True→255, False→0. The
test suite asserts every generated image contains only `{0, 255}`.

The core trick (`_PatternSet._render`): compute a `(height, width)` boolean array,
then turn it into an RGB image. Everything else is just "how do I compute the
mask."

### Rule 3: mind the numpy axis order

Image arrays are indexed `[row, col] = [y, x]`, the opposite of how you say
"x, y." A vertical line is `mask[:, x0:x1] = True` (all rows, some columns); a
horizontal line is `mask[y0:y1, :]`. Internalize this and the geometry is easy.

### The pattern math

- **Solid**: all-True or all-False mask.
- **Lines at any angle** (`_lines`): the elegant generalization is a
  **perpendicular projection**. A line at angle `a` is the set of pixels whose
  projection `p(x,y) = x·sin(a) + y·cos(a)` lies within `±width/2` of a target
  value. For `a=90°` (vertical) `p = x`; for `a=0°` (horizontal) `p = y`. Multiple
  lines are just several bands OR-ed into one mask, which is why a single `on`
  flag toggles the whole group.
- **Circle / ring**: `dist² = (x−cx)² + (y−cy)²`; disk = `dist² ≤ r²`, ring =
  `inner² ≤ dist² ≤ outer²`. Built with `np.ogrid` (memory-efficient broadcasting).

### The diamond geometry — why 45° means "horizontal"

The DLP6500's mirrors tilt **±12° about their diagonal**, and the array is a
"diamond" layout — effectively the chip is rotated 45° relative to the optical
table. The consequence for pattern drawing:

> **A line that should be horizontal in your experiment is a 45° line in pixel
> coordinates.**

That's why the wavelength-mixing patterns use `orientation="45"` (or `"-45"`,
depending on mount handedness) to get lab-horizontal stripes. For angled lines,
`offset` (center-relative perpendicular shift, in pixels) is the natural "scan
the stripe up/down" knob: `offset=0` is the line through the chip center, and
± moves it perpendicular to itself.

### Polarity

`on=True` draws a bright shape on a dark field; `on=False` inverts it (dark shape
on a bright field). Which one you want depends on your optics; `set_flip_longaxis`
/ `set_flip_shortaxis` handle a mirrored image.

---

## 5. The debugging story — a method you can reuse

Getting thin diagonal lines to display reliably took a long chase. The patterns
in *how* we found each bug are more valuable than the bugs themselves.

### The method: isolate the layer

When a pattern didn't show, the question was always: **which layer is wrong —
the image, the encoding, or the transport/hardware?** The trick is to find a test
that holds two layers fixed and varies one:

- **Is the image right?** Save the PIL image to PNG and look; assert it's binary
  and the right size. (Pure, hardware-free — that's why `tests/test_patterns.py`
  exists.)
- **Is the encoding right?** Round-trip it through a decoder and compare to the
  original. Better: get the **vendor's reference** and diff against it.
- **Is it the transport/hardware?** Change *only* the encoding (e.g.
  compressed vs uncompressed) and see if the symptom moves.

### Tools that did the heavy lifting

1. **A mocked HID device** (`/tmp` test): records every byte we'd send, so we can
   assert the command sequence, framing, chunk sizes, and header offline.
2. **The vendor's source as ground truth**: TI's GUI source (`compress.c`,
   `splash.c`, `API.c`, `usb.c`). We **compiled `compress.c`** and diffed its
   output against ours to *prove* our encoder was byte-identical.
3. **The "control" experiment**: uploading the *same* image uncompressed. When
   uncompressed worked but RLE didn't, that single fact eliminated the image, the
   header, and the upload path all at once — leaving only "something about RLE."
4. **The reference GUI** as an oracle: the TI GUI displayed our exact images
   correctly, proving the hardware *could* show them.

### The bugs, and what caught each one

1. **pyusb→HID** (design): chose `hid` to avoid driver swaps.
2. **Inverted warning**: an "error flag set!" message that fired on *success* —
   the condition was backwards. (Read the code, not the message.)
3. **Empty/stale reads**: fixed with flush-before-read + sequence matching +
   retry; root cause was HID buffering write-acks (a pyusb-vs-HID difference).
4. **Wrong data opcode**: the upstream library reused the *init* opcode `0x1A2A`
   for the data load instead of `0x1A2B`. Caught by noticing the secondary
   controller used `0x1A2C`/`0x1A2D` (a matched pair) but the primary didn't.
5. **Misaligned length fields**: `bits_to_bytes(number_to_bits(v, 10))` with a
   bit-count that isn't a multiple of 8 produced garbage for values > 255 (a
   504-byte chunk declared its length as 32256). Fixed with explicit
   little-endian packing — and caught by *asserting the decoded field equals the
   value*, not just that the opcode was present.
6. **Half-screen image**: auto "dual controller" detection split the image and
   sent half to a nonexistent second controller. Fixed by defaulting to single.
7. **The "OTF weirdness" rabbit hole**: per-line EOL markers, the `Pixel_format=0`
   header byte, copy-previous behavior — we matched all of them to TI by
   compiling `compress.c` and byte-diffing. This **proved the encoder innocent**,
   which was essential: it stopped us fixing the wrong thing.
8. **The real root cause**: the **reply/ack bit on data-load commands**. The
   decisive clue was "uncompressed works, *both* RLE types fail, and the GUI
   works." Uncompressed differs from RLE only in that the device *decompresses on
   the fly*; and TI's `LCR_SendMsg` had a one-line comment — *"Disable Read back
   for ... PATMEM_LOAD_DATA commands"* — clearing exactly that bit. We were
   requesting an ack mid-stream and corrupting the decompression.

### The transferable lessons

- **Bisect the pipeline.** Don't guess — find the experiment that splits the
  problem in half.
- **"Same input, different output → it's not the hardware."** When changing the
  *encoding* changed which offsets failed, the displayed image was identical, so
  the fault had to be in our bytes/transport, not the optics. That reasoning
  redirected the whole investigation.
- **Get a ground-truth reference and compare to it exactly.** Compiling the
  vendor's encoder and diffing byte-for-byte ended weeks of speculation in one
  command.
- **Read the vendor's code, including the comments.** The fix was a single bit,
  documented in a one-line comment we eventually found.
- **Beware "valid but not identical."** Our RLE round-tripped through a *software*
  decoder perfectly yet failed on *silicon* — passing your own round-trip test
  isn't proof of hardware compatibility.

---

## 6. Quick reference

```python
from dlpc900_hid import DMD, patterns

with DMD() as dmd:                       # opens HID; dmd.compression = 'erle' by default
    mw = patterns.MixWavelengths()       # experiment 1: solids + 1/2/3 lines
    hb = patterns.HBBrush()              # experiment 2: circle + ring

    # one-shot display
    dmd.show_image_otf(mw.one_line(orientation="45", offset=0, width=20))

    # fast multi-pattern: encode once, switch in ~ms
    dmd.load_patterns([mw.solid_on(), hb.circle(radius=270), mw.one_line(960, 20)])
    dmd.display_pattern(1)               # show the circle
```

Compression modes (`dmd.compression` or per-call `compression=`):

| value | type | use |
|-------|------|-----|
| `'erle'` | enhanced RLE (2) | **default** — smallest/fastest |
| `'rle'`  | basic RLE (1) | fallback if a unit mis-decodes enhanced |
| `'none'` | uncompressed (0) | last resort; reliable but ~6 MB/upload |

Key opcodes: `0x1A1B` display mode · `0x1A24` start/stop · `0x1A34` LUT entry ·
`0x1A31` LUT config · `0x1A2A` init BMP load · `0x1A2B` BMP data (no ack!).

References: TI DLPC900 Programmer's Guide **DLPU018** (esp. §2.4.3 compression,
§2.4.4 pattern display); TI DLP LightCrafter GUI source (`compress.c`,
`splash.c`, `API.c`, `usb.c`); [Pycrafter6500](https://github.com/csi-dcsc/Pycrafter6500)
and [dlpyc900](https://github.com/WetenSchaap/dlpyc900) (the starting point).
