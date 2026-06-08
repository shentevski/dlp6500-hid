"""
Enhanced Run-Length Encoding (ERLE) for DLPC900 pattern images.

The DLPC900 expects "on-the-fly" / flash pattern images wrapped in a 48-byte
header and compressed with TI's Enhanced RLE scheme (BMP compression type 2).
This module produces that byte stream from a Pillow image.

This encoder is adapted from the `dlpyc900` project by Piet J.M. Swinkels
(https://github.com/WetenSchaap/dlpyc900), which is GPLv3 licensed; this file
is therefore distributed under the same terms. See the README for details.
"""
import struct
import numpy as np
from PIL import Image
from typing import Tuple


def _enc128(num: int) -> bytearray:
    """
    Encode a number (up to 32767) into 1 or 2 bytes using the variable-length
    integer scheme specified in the TI documentation.
    """
    if 0 <= num < 128:
        return bytearray([num])
    return bytearray([(num & 0x7f) | 0x80, num >> 7])


def _encode_row(row: np.ndarray, prev_row: np.ndarray) -> bytearray:
    """Encode a single row using the TI Enhanced RLE logic."""
    width = len(row)
    compressed = bytearray()

    if prev_row is None:
        same_prev = np.zeros(width, dtype=bool)
    else:
        same_prev = (row == prev_row)

    if width > 1:
        same = (row[:-1] == row[1:])
        same_either = np.logical_or(same_prev[:-1], same)
    else:
        same = np.zeros(0, dtype=bool)
        same_either = np.zeros(0, dtype=bool)

    j = 0
    while j < width:
        # 1. Copy n pixels from previous line -> 0x00 0x01 [n]
        if same_prev[j]:
            run_len = 1
            while j + run_len < width and same_prev[j + run_len]:
                run_len += 1
            compressed += b'\x00\x01'
            compressed += _enc128(run_len)
            j += run_len

        # 2. Repeat single pixel n times -> [n] [B G R]
        elif j < width - 1 and same[j]:
            run_len = 2
            while j + run_len < width:
                if same[j + run_len - 1]:
                    run_len += 1
                else:
                    break
            compressed += _enc128(run_len)
            compressed += struct.pack('>I', int(row[j]))[1:4]
            j += run_len

        # 3. Single uncompressed pixel -> 0x01 [B G R]
        elif j >= width - 2 or same_either[j]:
            compressed += b'\x01'
            compressed += struct.pack('>I', int(row[j]))[1:4]
            j += 1

        # 4. Multiple uncompressed pixels -> 0x00 [n] [B G R ...]
        else:
            pixels = bytearray()
            pixels.extend(struct.pack('>I', int(row[j]))[1:4])
            j += 1
            while j < width - 1 and not same_either[j]:
                pixels.extend(struct.pack('>I', int(row[j]))[1:4])
                j += 1
            if j < width:
                pixels.extend(struct.pack('>I', int(row[j]))[1:4])
                j += 1
            count = len(pixels) // 3
            compressed += b'\x00'
            compressed += _enc128(count)
            compressed += pixels

    # End-of-line marker
    compressed += b'\x00\x00'
    return compressed


def enhanced_rle_encode(image: Image.Image, vertical_rle: bool = False) -> bytes:
    """
    Encode a Pillow image into the DLPC900 ERLE byte stream (48-byte header +
    compressed payload). The image is interpreted as RGB; convert beforehand.

    vertical_rle : if True, use the "copy from previous row" command (better
        compression). Default False -> horizontal RLE only. Some DLPC900 setups
        mis-display thin diagonal patterns that rely on copy-previous-row
        (vertical lines, which copy whole rows, are unaffected); horizontal-only
        is the robust mode and only slightly larger.
    """
    if image.mode != 'RGB':
        image = image.convert('RGB')

    width, height = image.size
    arr = np.asarray(image)
    # Pack to 0x00BBGGRR per pixel.
    img_uint32 = (arr[:, :, 2].astype(np.uint32) << 16) | \
                 (arr[:, :, 1].astype(np.uint32) << 8) | \
                 (arr[:, :, 0].astype(np.uint32))

    encoded = bytearray(48)  # header placeholder

    prev_row = None
    for y in range(height):
        row = img_uint32[y]
        # Passing prev_row=None disables the "copy previous row" command.
        encoded += _encode_row(row, prev_row if vertical_rle else None)
        prev_row = row

    # End-of-image marker + pad to 4-byte boundary.
    encoded += b'\x00\x01\x00'
    encoded += bytearray((-len(encoded)) % 4)

    # Fill header.
    encoded[0:4] = b'Spld'
    struct.pack_into('<H', encoded, 4, width)
    struct.pack_into('<H', encoded, 6, height)
    struct.pack_into('<I', encoded, 8, len(encoded) - 48)
    encoded[12:20] = b'\xFF' * 8
    encoded[20:24] = b'\x00\x00\x00\x00'
    encoded[24] = 0x00
    encoded[25] = 0x02   # Enhanced RLE
    encoded[26] = 0x01
    return bytes(encoded)
