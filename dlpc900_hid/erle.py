"""
Enhanced Run-Length Encoding (ERLE) for DLPC900 pattern images.

The DLPC900 wants pattern images wrapped in a 48-byte "Spld" header and
compressed. This is a faithful port of Texas Instruments' own encoder from the
DLP LightCrafter 6500/9000 GUI source (``compress.c`` :: ``RLE_CompressBMPSpl``
and ``splash.c`` :: ``SPL_Header_t`` / ``SPL_ConvImageToSplash``), so the byte
stream matches what the TI GUI produces -- which the DMD hardware decodes
correctly (an earlier hand-rolled encoder mis-displayed thin diagonal patterns).

Key points matched from TI's source:
  * Enhanced RLE (compression type 2): repeat / copy-from-previous-line /
    uncompressed runs, with copy preferred when ``Copy >= Repeat``.
  * NO per-line end-of-line (0x00 0x00) markers (TI's enhanced encoder omits
    them; emitting them was the bug that dropped thin diagonal lines).
  * Header: Pixel_format=1 (24-bit packed), Compression=2, ByteOrder=1,
    IsLeftImage=1, Subimg_offset/end=0xFFFFFFFF, Bg_color=0.
  * Variable-length count: <128 -> 1 byte; >=128 -> (n|0x80)&0xFF, (n>>7)&0xFF.
"""
import struct
import numpy as np
from PIL import Image


def _add_count(out: bytearray, count: int):
    """TI AddCount(): variable-length run count (1 or 2 bytes)."""
    if count < 128:
        out.append(count & 0xFF)
    else:
        out.append((count | 0x80) & 0xFF)
        out.append((count >> 7) & 0xFF)


def _emit_raw(out: bytearray, wire: np.ndarray, start: int, count: int):
    """
    Flush a run of `count` uncompressed pixels (TI EncodePix Repeat==0).
    A single raw pixel is emitted as a repeat-1 command, exactly as TI does.
    """
    if count == 1:
        _add_count(out, 1)                       # repeat-1 == one literal pixel
        out += wire[start].tobytes()
    else:
        out += b'\x00'
        _add_count(out, count)
        out += wire[start:start + count].tobytes()


def enhanced_rle_encode(image: Image.Image, vertical_rle: bool = False) -> bytes:
    """
    Encode a Pillow image into the DLPC900 enhanced-RLE 'Spld' byte stream
    (header + control bytes ported from TI's compress.c / splash.c).

    vertical_rle : use the copy-from-previous-line command. TI's encoder uses it
        for best compression, BUT this DLPC900/DLP6500 silicon mis-displays thin
        diagonal patterns that use copy-previous (periodic dropouts), so we
        DEFAULT IT OFF (horizontal RLE only) -- still small and fast, and avoids
        the hardware quirk. Set True to re-enable maximal compression.
    """
    if image.mode != 'RGB':
        image = image.convert('RGB')

    width, height = image.size
    arr = np.asarray(image)                                  # (H, W, 3) RGB
    R = arr[:, :, 0].astype(np.uint32)
    G = arr[:, :, 1].astype(np.uint32)
    B = arr[:, :, 2].astype(np.uint32)
    key = (R << 16) | (G << 8) | B                           # equality key/pixel
    # On-wire pixel bytes in TI's order (AddPixel writes Pix[2],Pix[0],Pix[1];
    # with TI's internal B,G,R that is R,B,G). For grayscale all three are equal.
    wire = np.stack([arr[:, :, 0], arr[:, :, 2], arr[:, :, 1]],
                    axis=-1).astype(np.uint8)                # (H, W, 3) = [R,B,G]

    out = bytearray(48)                                      # header placeholder
    prev = None
    for y in range(height):
        k = key[y]
        w = wire[y]
        p = prev if vertical_rle else None
        x = 0
        raw = 0
        while x < width:
            val = k[x]
            tail = k[x + 1:]
            nz = np.flatnonzero(tail != val)
            repeat = int(nz[0]) + 1 if nz.size else (width - x)

            if p is not None:
                nzc = np.flatnonzero(k[x:] != p[x:])
                copy = int(nzc[0]) if nzc.size else (width - x)
            else:
                copy = 0

            if copy > 0 and copy >= repeat:
                if raw:
                    _emit_raw(out, w, x - raw, raw)
                    raw = 0
                out += b'\x00\x01'                           # copy-from-prev
                _add_count(out, copy)
                x += copy
            elif repeat > 1:
                if raw:
                    _emit_raw(out, w, x - raw, raw)
                    raw = 0
                _add_count(out, repeat)                      # repeat pixel
                out += w[x].tobytes()
                x += repeat
            else:
                x += 1
                raw += 1
        if raw:
            _emit_raw(out, w, x - raw, raw)
        prev = k

    out += b'\x00\x01\x00'                                   # end of image

    # ---- 48-byte SPL_Header_t (see TI splash.c) ----
    out[0:4] = b'Spld'
    struct.pack_into('<H', out, 4, width)                    # Image_width
    struct.pack_into('<H', out, 6, height)                   # Image_height
    struct.pack_into('<I', out, 8, len(out) - 48)            # Byte_count
    struct.pack_into('<I', out, 12, 0xFFFFFFFF)              # Subimg_offset
    struct.pack_into('<I', out, 16, 0xFFFFFFFF)              # Subimg_end
    struct.pack_into('<I', out, 20, 0x00000000)              # Bg_color
    out[24] = 1                                              # Pixel_format = packed RGB
    out[25] = 2                                              # Compression = enhanced RLE
    out[26] = 1                                              # ByteOrder
    out[29] = 1                                              # IsLeftImage
    return bytes(out)


def uncompressed_encode(image: Image.Image) -> bytes:
    """
    Encode an image with NO compression (compression type 0): 48-byte header +
    raw pixel bytes. Mainly a diagnostic (~6 MB for 1920x1080, slow to upload).
    """
    if image.mode != 'RGB':
        image = image.convert('RGB')

    width, height = image.size
    arr = np.asarray(image)
    # Match TI's on-wire pixel order [R, B, G] (irrelevant for grayscale).
    payload = np.stack([arr[:, :, 0], arr[:, :, 2], arr[:, :, 1]],
                       axis=-1).astype(np.uint8).tobytes()

    out = bytearray(48) + bytearray(payload)

    out[0:4] = b'Spld'
    struct.pack_into('<H', out, 4, width)
    struct.pack_into('<H', out, 6, height)
    struct.pack_into('<I', out, 8, len(out) - 48)
    struct.pack_into('<I', out, 12, 0xFFFFFFFF)
    struct.pack_into('<I', out, 16, 0xFFFFFFFF)
    struct.pack_into('<I', out, 20, 0x00000000)
    out[24] = 1
    out[25] = 0                                              # uncompressed
    out[26] = 1
    out[29] = 1
    return bytes(out)
