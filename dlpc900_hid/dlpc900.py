"""
USB-HID driver for Texas Instruments DLPC900-based DMDs
(DLP6500, DLP9000, DLP670S, DLP500YX, DLP5500 / Thorlabs & TI LightCrafter EVMs).

Unlike the upstream `dlpyc900` project (which uses pyusb and therefore requires
replacing the Windows driver with libusb via Zadig), this module talks to the
device through the `hid` package. The DLPC900 enumerates as a standard USB-HID
device, so on Windows it works with the OS's built-in HID driver -- no driver
installation required.

Protocol logic (command opcodes, payload packing, ERLE upload sequence) is based
on the dlpyc900 project (GPLv3) and TI's DLPC900 user guide (DLPU018).

Install the transport with either:
    pip install hidapi      # cython-hidapi, ships prebuilt wheels (recommended)
or
    pip install hid         # pyhidapi; needs the hidapi shared library present
Both expose the ``hid.device()`` API used here.
"""
from __future__ import annotations

import time
import math
import warnings

import hid
import PIL.Image as Image

from .erle import (enhanced_rle_encode, basic_rle_encode, uncompressed_encode)

# image encoders by compression name (header compression-type byte in parens)
_ENCODERS = {
    'rle': basic_rle_encode,        # type 1 -- basic RLE (DLP6500-safe default)
    'erle': enhanced_rle_encode,    # type 2 -- enhanced RLE (smallest, but some
                                    #            DLP6500 units mis-decode it)
    'none': uncompressed_encode,    # type 0 -- uncompressed (~6 MB, slow)
}
from .errors import DMDError

# TI / DLPC900 USB identifiers.
VENDOR_ID = 0x0451
PRODUCT_ID = 0xC900

# HID report parameters for the DLPC900.
REPORT_ID = 0x00          # device uses unnumbered reports
REPORT_SIZE = 64          # 64-byte reports in each direction
READ_TIMEOUT_MS = 2000
# Small positive timeout for draining the input buffer. NOT 0: on the Windows
# hidapi build a 0 timeout blocks forever instead of polling non-blockingly.
FLUSH_TIMEOUT_MS = 10

HARDWARE_CODES = {
    0x00: "unknown", 0x01: "DLP6500", 0x02: "DLP9000",
    0x03: "DLP670S", 0x04: "DLP500YX", 0x05: "DLP5500",
}


# --------------------------------------------------------------------------- #
# Bit/byte helpers (kept compatible with the upstream dlpyc900 naming).
# --------------------------------------------------------------------------- #
def bits_to_bytes(bits: str) -> list[int]:
    """Convert a string of bits to a little-endian list of byte values."""
    a = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    a.reverse()
    return a


def number_to_bits(a: int, bitlen: int = 8) -> str:
    """Convert a number to a zero-padded binary string of given bit length."""
    return format(a, '0{}b'.format(bitlen))


def bits_to_bools(a: str) -> tuple[int, ...]:
    """Convert a bit string ('01101') to a tuple of ints (0,1,1,0,1)."""
    return tuple(map(int, a))


def list_to_int(byte_list) -> int:
    """Convert a little-endian list of bytes (0-255) to an integer."""
    rev = list(byte_list)
    rev.reverse()
    if not all(0 <= b <= 255 for b in rev):
        raise ValueError("All elements must be in the range 0-255")
    return int.from_bytes(bytes(rev), byteorder='big', signed=False)


def bytes_to_text(byte_list) -> str:
    """Decode a NUL-terminated ASCII byte list to a string."""
    out = ''
    for c in byte_list:
        if c == 0:
            break
        out += chr(c)
    return out


def parse_reply(reply):
    """
    Split a raw 64-byte reply into:
    (error_flag, flag_byte, sequence_byte, length, data_tuple).
    """
    if reply is None or len(reply) == 0:
        return None
    flag_byte = number_to_bits(reply[0])
    sequence_byte = reply[1]
    length = reply[2] | (reply[3] << 8)
    data = reply[4:4 + length]
    error_flag = (reply[0] & 0x20) != 0
    return error_flag, flag_byte, sequence_byte, length, tuple(data)


def list_hid_devices() -> list[dict]:
    """Return all connected HID devices (handy for confirming the DMD is seen)."""
    return hid.enumerate()


class DMD:
    """
    Controller for a DLPC900-based DMD over USB-HID.

    Typical use::

        from dlpc900_hid import DMD, patterns
        with DMD() as dmd:
            model = dmd.get_hardware()[0]
            w, h = dmd.get_resolution()
            img = patterns.circle(w, h)
            dmd.show_image_otf(img, exposure_us=1_000_000)
    """

    def __init__(self, vendor_id: int = VENDOR_ID, product_id: int = PRODUCT_ID,
                 serial: str | None = None):
        self.device = hid.device()
        try:
            self.device.open(vendor_id, product_id, serial) if serial else \
                self.device.open(vendor_id, product_id)
        except (OSError, IOError) as exc:
            raise DMDError(
                f"Could not open DMD (VID={vendor_id:#06x}, PID={product_id:#06x}). "
                "Is it plugged in and powered? On Windows no special driver is "
                "needed for HID, but make sure another program (e.g. the TI GUI) "
                "isn't holding the device open."
            ) from exc

        try:
            self.device.set_nonblocking(0)
        except Exception:
            pass

        self.current_mode = "pattern"
        self.display_modes = {'video': 0, 'pattern': 1, 'video-pattern': 2, 'otf': 3}
        self.display_modes_inv = {v: k for k, v in self.display_modes.items()}

        # Read tuning: the DLPC900 occasionally returns an empty reply if read
        # too soon, so we wait then retry the query a few times.
        self.read_delay = 0.05      # seconds to wait after writing, before reading
        self.read_retries = 6       # how many times to re-issue a read that comes back empty

        # Cache for the fast load_patterns / display_pattern workflow: a list
        # of pre-encoded patterns, each a list of (controller, encoded_bytes).
        self._patterns: list = []
        self._pattern_dual = False

        # Pattern image compression: 'erle' (enhanced, type 2 -- smallest/fastest)
        # / 'rle' (basic, type 1) / 'none' (uncompressed). Enhanced is the default
        # now that data-load acks are disabled (the real cause of RLE dropouts).
        self.compression = 'erle'

        # Confirm the link actually works.
        try:
            self.hardware = self.get_hardware()[0]
        except DMDError:
            raise
        except Exception as exc:
            raise DMDError("Connection to DMD opened but no valid reply received.") from exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        try:
            self.standby()
        finally:
            self.close()

    def close(self):
        """Close the HID handle."""
        try:
            self.device.close()
        except Exception:
            pass

    # ----------------------------------------------------------------------- #
    # Low-level HID transport
    # ----------------------------------------------------------------------- #
    def _write_report(self, buffer: list[int]):
        """Write one 64-byte report (prepending the HID report id)."""
        report = bytes([REPORT_ID]) + bytes(buffer)
        try:
            self.device.write(report)
        except (OSError, IOError):
            # Occasional transient failure; brief wait + retry fixes most cases.
            time.sleep(0.1)
            self.device.write(report)

    def _read_report(self) -> list[int]:
        """Read one report from the device (blocking up to READ_TIMEOUT_MS)."""
        data = self.device.read(REPORT_SIZE, READ_TIMEOUT_MS)
        return list(data)

    def _flush_input(self):
        """
        Drain any buffered input reports.

        The DLPC900 sets the 'reply' bit on every command (including writes),
        so each write leaves an unread ack in the OS HID input ring buffer.
        Over pyusb those get dropped, but the Windows HID driver buffers them,
        which otherwise makes the next read return a stale reply. Flush them
        with non-blocking (timeout 0) reads before issuing a fresh query.
        """
        for _ in range(128):
            try:
                if not self.device.read(REPORT_SIZE, FLUSH_TIMEOUT_MS):
                    break
            except (OSError, IOError):
                break

    def _build_reports(self, mode: str, sequence_byte: int, command: int,
                       payload: list[int], request_reply: bool = True) -> list[list[int]]:
        """Build the list of 64-byte reports for one command."""
        buffer: list[int] = []

        # Flag byte: bit7 = read, bit6 = reply/ack requested.
        # Reads always need a reply; writes request one EXCEPT data-load commands
        # (TI clears the ack bit for PATMEM_LOAD_DATA so on-the-fly decompression
        # isn't disturbed mid-stream -- requesting an ack there corrupts RLE).
        read = (mode == 'r')
        reply = read or request_reply
        buffer.append((0x80 if read else 0) | (0x40 if reply else 0))

        # Sequence byte.
        buffer.append(sequence_byte)

        # Length (payload + 2 command bytes), little-endian.
        temp = bits_to_bytes(number_to_bits(len(payload) + 2, 16))
        buffer.append(temp[0])
        buffer.append(temp[1])

        # Command bytes, little-endian.
        buffer.append(command & 0xFF)
        buffer.append((command >> 8) & 0xFF)

        reports: list[list[int]] = []
        if len(buffer) + len(payload) < (REPORT_SIZE + 1):
            buffer.extend(payload)
            buffer.extend([0x00] * (REPORT_SIZE - len(buffer)))
            reports.append(buffer)
        else:
            # First report carries the 6-byte header + up to 58 payload bytes,
            # the rest are sent as full 64-byte reports.
            remaining = list(payload)
            buffer.extend(remaining[:58])
            reports.append(buffer)
            remaining = remaining[58:]
            while remaining:
                chunk = remaining[:REPORT_SIZE]
                remaining = remaining[REPORT_SIZE:]
                if len(chunk) < REPORT_SIZE:
                    chunk.extend([0x00] * (REPORT_SIZE - len(chunk)))
                reports.append(chunk)
        return reports

    def send_command(self, mode: str, sequence_byte: int, command: int,
                     payload: list[int] | None = None, request_reply: bool = True):
        """
        Send a command to the DMD.

        Parameters
        ----------
        mode : str
            'r' to read (a reply is returned), 'w' to write.
        sequence_byte : int
            Arbitrary 1-byte tag to match replies to commands.
        command : int
            16-bit command opcode from the DLPC900 user guide (e.g. 0x1A1B).
        payload : list[int], optional
            Data bytes for the command.
        request_reply : bool
            For writes, whether to set the ack/reply bit. Clear it for pattern
            data-load commands (matches TI; avoids corrupting RLE decompression).
        """
        if payload is None:
            payload = []

        reports = self._build_reports(mode, sequence_byte, command, payload,
                                      request_reply=request_reply)

        if mode != 'r':
            for report in reports:
                self._write_report(report)
            return None

        # Read mode: flush stale write-acks, issue the query, and accept only a
        # non-empty reply whose echoed sequence byte matches. Retry otherwise.
        answer = None
        for attempt in range(self.read_retries):
            self._flush_input()
            for report in reports:
                self._write_report(report)
            time.sleep(self.read_delay)
            answer = parse_reply(self._read_report())
            # answer = (error_flag, flag_byte, sequence_byte, length, data)
            if (answer is not None and answer[3] > 0
                    and answer[2] == sequence_byte):
                if answer[0]:   # error flag (reply flag-byte bit 5, 0x20)
                    warnings.warn(
                        "DMD reply has its error flag (0x20) set for command "
                        f"{command:#06x}.")
                return answer

        raise DMDError(
            f"No valid reply from DMD for command {command:#06x} after "
            f"{self.read_retries} attempts (empty or mismatched replies).")

    # ----------------------------------------------------------------------- #
    # Status / identity (user guide section 2.1)
    # ----------------------------------------------------------------------- #
    def get_hardware(self) -> tuple[str, str]:
        """Return (hardware product name, firmware tag string)."""
        ans = self.send_command('r', 10, 0x0206)
        hw = ans[-1][0]
        fw = ans[-1][1:]
        return HARDWARE_CODES.get(hw, "undocumented hardware"), bytes_to_text(fw)

    def get_hardware_status(self) -> tuple[int, ...]:
        """Return the 8 hardware-status bits (see user guide 2.1.1)."""
        ans = self.send_command('r', 10, 0x1A0A)
        return bits_to_bools(number_to_bits(ans[-1][0], 8))

    def get_hardware_status_for_humans(self) -> str:
        """Human-readable hardware-status report (user guide Table 2-2)."""
        b = self.get_hardware_status()
        msg = ''
        msg += ("Internal Initialization Successful\n" if b[0]
                else "Internal Initialization Error\n")
        msg += ("System is compatible\n" if not b[1]
                else "Incompatible Controller/DMD or wrong firmware\n")
        msg += ("DMD Reset Controller has no errors\n" if not b[2]
                else "DMD Reset Controller Error\n")
        msg += ("No Forced Swap Errors\n" if not b[3]
                else "Forced Swap Error occurred\n")
        msg += ("No Secondary Controller Present\n" if b[4]
                else "Secondary Controller Present and Ready\n")
        msg += ("Sequencer Abort Status reports no errors\n" if not b[6]
                else "Sequencer detected an error condition that caused an abort\n")
        msg += ("Sequencer reports no errors\n" if not b[7]
                else "Sequencer detected an error\n")
        return msg

    def get_main_status(self) -> tuple[int, ...]:
        """
        Return main-status bits:
        0 parked, 1 sequencer running, 2 video frozen,
        3 external source locked, 4 port1 syncs valid, 5 port2 syncs valid.
        """
        ans = self.send_command('r', 10, 0x1A0C)
        return bits_to_bools(number_to_bits(ans[-1][0], 8))[:6]

    def is_dual_controller(self) -> bool:
        """True if the DMD has a secondary controller (e.g. dual-DLPC900 DLP9000)."""
        # Status bit 4 == 0 means a secondary controller is present and ready.
        return self.get_hardware_status()[4] == 0

    # ----------------------------------------------------------------------- #
    # Power management (section 2.3.1)
    # ----------------------------------------------------------------------- #
    def standby(self):
        """Put the DMD into standby (stops any running pattern first)."""
        try:
            self.stop_pattern()
        except Exception:
            pass
        self.send_command('w', 0x00, 0x0200, [1])

    def wakeup(self):
        """Wake the DMD from standby."""
        self.send_command('w', 0x00, 0x0200, [0])

    def reset(self):
        """Soft-reset the DMD controller."""
        self.send_command('w', 0x00, 0x0200, [2])

    def idle_on(self):
        """Enter idle mode (stops any running pattern first)."""
        try:
            self.stop_pattern()
        except Exception:
            pass
        self.send_command('w', 0x00, 0x0201, [1])

    def idle_off(self):
        """Leave idle mode."""
        self.send_command('w', 0x00, 0x0201, [3])

    def get_current_powermode(self) -> str:
        """Return 'normal', 'idle', or 'standby'."""
        idle = self.send_command('r', 0x00, 0x0201)[-1][0]
        sleep = self.send_command('r', 0x00, 0x0200)[-1][0]
        if sleep == 1:
            return "standby"
        if idle == 0:
            return "normal"
        if idle == 1:
            return "idle"
        return "undocumented state"

    # ----------------------------------------------------------------------- #
    # Input source / image flips (section 2.3)
    # ----------------------------------------------------------------------- #
    def set_input_source(self, source: int = 0, bitdepth: int = 0):
        """
        Select input source.
        source: 0 parallel, 1 internal test, 2 flash, 3 solid curtain.
        bitdepth: 0 30-bit, 1 24-bit, 2 20-bit, 3 16-bit.
        """
        payload = (source & 0x07) | ((bitdepth & 0x03) << 3)
        self.send_command('w', 1, 0x1A00, [payload])

    def set_flip_longaxis(self, flip: bool):
        """Flip the image along the long axis."""
        self.send_command('w', 0, 0x1008, [int(flip)])

    def set_flip_shortaxis(self, flip: bool):
        """Flip the image along the short axis."""
        self.send_command('w', 0, 0x1009, [int(flip)])

    # ----------------------------------------------------------------------- #
    # Display mode & resolution (section 2.4)
    # ----------------------------------------------------------------------- #
    def set_display_mode(self, mode: str):
        """
        Set display mode: 'video', 'pattern', 'video-pattern', or 'otf'
        (on-the-fly).
        """
        if mode not in self.display_modes:
            raise ValueError(f"mode '{mode}' unknown")
        if mode == 'video-pattern' and self.current_mode != 'video':
            raise ValueError(
                "To switch to video-pattern mode the system must be in video "
                "mode with a locked source first.")
        self.send_command('w', 0x00, 0x1A1B, [self.display_modes[mode]])
        time.sleep(0.5)
        new_mode = self.get_display_mode()
        if new_mode != mode:
            raise DMDError(f"Mode activation failed (asked '{mode}', got '{new_mode}').")

    def get_display_mode(self) -> str:
        """Return the current display mode name."""
        ans = self.send_command('r', 0x00, 0x1A1B)
        value = ans[-1][0]
        if value not in self.display_modes_inv:
            raise DMDError(f"DMD reported unknown display-mode value {value}.")
        self.current_mode = self.display_modes_inv[value]
        return self.current_mode

    def get_display_resolution(self) -> tuple[int, ...]:
        """
        Return (in_fac, in_fal, in_hr, in_vr, out_fac, out_fal, out_hr, out_vr).
        Indices 6 and 7 are the output horizontal/vertical resolution.
        """
        ans = self.send_command('r', 12, 0x1000)[-1]
        return tuple(list_to_int(ans[i:i + 2]) for i in range(0, 16, 2))

    def get_resolution(self) -> tuple[int, int]:
        """Return the DMD's output (width, height) in pixels."""
        r = self.get_display_resolution()
        return r[6], r[7]

    # ----------------------------------------------------------------------- #
    # Pattern sequence control (section 2.4.4.3)
    # ----------------------------------------------------------------------- #
    def start_pattern(self):
        """Start the pattern display sequence."""
        self.send_command('w', 5, 0x1A24, [2])

    def pause_pattern(self):
        """Pause the pattern display sequence."""
        self.send_command('w', 5, 0x1A24, [1])

    def stop_pattern(self):
        """Stop the pattern display sequence."""
        self.send_command('w', 5, 0x1A24, [0])

    def configure_pattern_from_LUT(self, nr_of_LUT_entries: int = 1,
                                   nr_of_patterns_to_display: int = 0):
        """
        Pattern Display LUT Control (0x1A31). Display nr_of_LUT_entries entries;
        nr_of_patterns_to_display = 0 means loop forever.
        """
        # 2-byte LE entry count + 4-byte LE repeat count.
        payload = [nr_of_LUT_entries & 0xFF, (nr_of_LUT_entries >> 8) & 0xFF,
                   nr_of_patterns_to_display & 0xFF,
                   (nr_of_patterns_to_display >> 8) & 0xFF,
                   (nr_of_patterns_to_display >> 16) & 0xFF,
                   (nr_of_patterns_to_display >> 24) & 0xFF]
        self.send_command('w', 1, 0x1A31, payload)

    def setup_pattern_LUT_definition(self, pattern_index: int = 0,
                                     exposuretime: int = 15000, darktime: int = 0,
                                     color: int = 7, bitdepth: int = 8,
                                     image_pattern_index: int = 0,
                                     bit_position: int = 0,
                                     trigger_in: bool = False,
                                     trigger_out_2_enabled: bool = True,
                                     extended_bit_depth: bool = False):
        """
        Add one entry to the pattern Look-Up Table (0x1A34).

        exposuretime / darktime in microseconds. color: 1 red, 2 green, 4 blue,
        7 all. image_pattern_index selects which uploaded image this entry shows.
        ``trigger_in`` False means the sequence free-runs (no external trigger
        needed to advance) -- what you want for simply displaying a pattern.

        Note: the options-byte packing here matches the validated Pycrafter6500
        implementation. The upstream dlpyc900 had an operator-precedence bug
        (``& 0x07 << 1`` parses as ``& (0x07 << 1)``) that silently dropped the
        shift, corrupting the bit-depth and color fields -- fixed below.
        """
        pattern_index_bytes = [pattern_index & 0xFF, (pattern_index >> 8) & 0xFF]
        exposuretime_bytes = [exposuretime & 0xFF, (exposuretime >> 8) & 0xFF,
                              (exposuretime >> 16) & 0xFF]

        # Options byte: bit0 = 1 (standard), bits1-3 = bitdepth-1,
        # bits4-6 = color, bit7 = wait-for-(external)-trigger.
        byte_5 = 0x01
        byte_5 |= ((bitdepth - 1) & 0x07) << 1
        byte_5 |= (color & 0x07) << 4
        byte_5 |= (int(trigger_in) & 0x01) << 7

        darktime_bytes = [darktime & 0xFF, (darktime >> 8) & 0xFF,
                          (darktime >> 16) & 0xFF]

        # Trigger-out byte: bit0 = trigger-out-2 enable, bit1 = extended bit depth.
        byte_9 = 0
        byte_9 |= int(trigger_out_2_enabled) & 0x01
        byte_9 |= (int(extended_bit_depth) & 0x01) << 1

        # bytes 10-11: 16-bit value = (bit_position << 11) | image_pattern_index.
        ipi = [image_pattern_index & 0xFF, (image_pattern_index >> 8) & 0xFF]
        bit_position_byte = (bit_position & 0x1F) << 3
        byte_10_11 = [ipi[0], ipi[1] | bit_position_byte]

        payload = (pattern_index_bytes + exposuretime_bytes + [byte_5] +
                   darktime_bytes + [byte_9] + byte_10_11)
        self.send_command('w', 1, 0x1A34, payload)

    # ----------------------------------------------------------------------- #
    # Image upload (section 2.4.4.4)
    # ----------------------------------------------------------------------- #
    def initialize_pattern_bmp_load(self, image_index: int, n_bytes: int,
                                    controller: int = 0):
        """Begin a BMP upload: declare image index and total byte count."""
        # 2-byte LE image index + 4-byte LE total size (incl. 48-byte header).
        payload = [image_index & 0xFF, (image_index >> 8) & 0xFF,
                   n_bytes & 0xFF, (n_bytes >> 8) & 0xFF,
                   (n_bytes >> 16) & 0xFF, (n_bytes >> 24) & 0xFF]
        command = {0: 0x1A2A, 1: 0x1A2C}.get(controller)
        if command is None:
            raise ValueError(f"{controller} is not a valid controller (0 or 1)")
        self.send_command('w', 0, command, payload)

    def pattern_bmp_load(self, encoded_chunk, controller: int = 0):
        """Send one chunk (<=504 bytes) of an ERLE-encoded image."""
        nr = len(encoded_chunk)
        if nr > 510:
            raise DMDError("Chunk too big; use <=504 byte chunks.")
        # 2-byte LE chunk length, then the chunk data.
        payload = [nr & 0xFF, (nr >> 8) & 0xFF] + list(encoded_chunk)
        # Data-load opcodes: 0x1A2B (primary) / 0x1A2D (secondary). These differ
        # from the *init* opcodes (0x1A2A / 0x1A2C). The upstream library wrongly
        # reused 0x1A2A for the primary data load, so the pixels never stored.
        command = {0: 0x1A2B, 1: 0x1A2D}.get(controller)
        if command is None:
            raise ValueError(f"{controller} is not a valid controller (0 or 1)")
        # request_reply=False: do NOT ask for an ack on data loads (matches TI;
        # an ack mid-stream corrupts the DMD's on-the-fly RLE decompression).
        self.send_command('w', 0, command, payload, request_reply=False)

    def _encode_image(self, image: Image.Image, dual_controller: bool = False,
                      compression: str | None = None):
        """
        Encode an image into a list of (controller, encoded_bytes) tasks, using
        `compression` (defaults to self.compression: 'rle' / 'erle' / 'none').
        For dual controllers the image is split left/right.
        """
        enc = _ENCODERS[compression or self.compression]
        if dual_controller:
            width, height = image.size
            half = width // 2
            return [(0, enc(image.crop((0, 0, half, height)))),
                    (1, enc(image.crop((half, 0, width, height))))]
        return [(0, enc(image))]

    def _send_encoded(self, image_index: int, encoded, controller: int = 0,
                      progress: bool = False):
        """Chunk an already-ERLE-encoded image and stream it to the controller."""
        max_payload_size = 504
        first_payload_size = 504
        data = list(encoded)
        nr_of_bytes = len(data)

        remainder = nr_of_bytes - first_payload_size
        nloops = 1 if remainder < 0 else math.ceil(remainder / max_payload_size) + 1

        chunks = []
        for i in range(nloops):
            if i == 0:
                start, end = 0, first_payload_size
            elif i == nloops - 1:
                start = (i - 1) * max_payload_size + first_payload_size
                chunks.append(data[start:])
                continue
            else:
                start = (i - 1) * max_payload_size + first_payload_size
                end = start + max_payload_size
            chunks.append(data[start:end])

        self.initialize_pattern_bmp_load(image_index, nr_of_bytes, controller=controller)
        for i, chunk in enumerate(chunks):
            self.pattern_bmp_load(chunk, controller=controller)
            if progress:
                print(f"Controller {controller}: uploaded chunk {i + 1}/{len(chunks)}")

    def upload_image(self, image_index: int, image: Image.Image,
                     dual_controller: bool = False, progress: bool = True,
                     compression: str | None = None):
        """
        Compress and upload an image to the device's pattern memory.

        image_index : 0-17. Fill from high to low (upload 17 before 16 ...).
        dual_controller : split the image across two controllers (e.g. DLP9000).
        compression : 'erle' / 'rle' / 'none' (defaults to self.compression).
        """
        for controller, encoded in self._encode_image(image, dual_controller,
                                                       compression=compression):
            self._send_encoded(image_index, encoded, controller=controller,
                               progress=progress)

    # ----------------------------------------------------------------------- #
    # High-level convenience: show one image in pattern-on-the-fly mode.
    # ----------------------------------------------------------------------- #
    def show_image_otf(self, image: Image.Image, exposure_us: int = 1_000_000,
                       dark_us: int = 0, bitdepth: int = 8, color: int = 7,
                       dual_controller: bool = False,
                       compression: str | None = None):
        """
        Display a single image in on-the-fly mode (looped indefinitely).

        This is a one-call wrapper around the full OTF sequence:
        stop -> set OTF mode -> define a 1-entry LUT -> configure LUT ->
        upload the image -> start. ``exposure_us`` is the on-time per cycle in
        microseconds. (Mirrors the proven Pycrafter6500 order; notably it does
        NOT set the input source to flash, which would show the flash patterns.)

        ``dual_controller`` defaults to False (correct for a single-DLPC900 board
        like the DLP6500 / DLPLCR900EVM, where the whole image goes to one
        controller). Set it True only on a dual-DLPC900 board (e.g. DLP9000),
        where the image is split into left/right halves across both controllers.

        ``compression`` overrides self.compression for this call
        ('erle' / 'rle' / 'none').
        """
        self.stop_pattern()
        self.set_display_mode("otf")
        self.setup_pattern_LUT_definition(
            pattern_index=0, exposuretime=exposure_us, darktime=dark_us,
            bitdepth=bitdepth, color=color, image_pattern_index=0)
        self.configure_pattern_from_LUT(nr_of_LUT_entries=1, nr_of_patterns_to_display=0)
        self.upload_image(0, image, dual_controller=dual_controller,
                          compression=compression)
        self.start_pattern()

    # ----------------------------------------------------------------------- #
    # Fast workflow: upload patterns once, then switch between them instantly.
    # ----------------------------------------------------------------------- #
    def enter_otf_mode(self):
        """
        Stop any running sequence and switch into pattern-on-the-fly mode.

        Call this once (it includes the ~0.5 s mode-change settle). After this,
        use upload_pattern() to load images and display_pattern() to switch
        between them quickly.
        """
        self.stop_pattern()
        self.set_display_mode("otf")

    def upload_pattern(self, index: int, image: Image.Image,
                       dual_controller: bool = False, progress: bool = False):
        """
        Upload one image into pattern-memory slot ``index`` (0-17) WITHOUT
        displaying it. This is the slow part (USB transfer), so do it up front.

        If you upload several, upload the HIGHEST index first (the controller
        requires descending order), e.g. upload index 1 before index 0.
        """
        self.upload_image(index, image, dual_controller=dual_controller,
                          progress=progress)

    def load_patterns(self, images, dual_controller: bool = False) -> int:
        """
        Prepare a set of images for fast display: ERLE-encode each one now
        (the slow ~100 ms/image step), cache the compressed bytes, and enter
        OTF mode once. display_pattern(i) then only streams the cached bytes.

        Note: the DLPC900 doesn't reliably hold multiple OTF images for pure
        index-switching, so display_pattern re-streams the selected image. With
        encoding cached, a switch is just the USB transfer (~0.1-0.2 s) and skips
        the ~0.5 s OTF mode-change that show_image_otf repeats.

        Returns the number of patterns cached. Up to 18.
        """
        images = list(images)
        if len(images) > 18:
            raise ValueError("At most 18 patterns supported.")
        # Pre-encode now; store list of (controller, encoded_bytes) tasks.
        self._patterns = [self._encode_image(img, dual_controller) for img in images]
        self._pattern_dual = dual_controller
        self.enter_otf_mode()
        return len(self._patterns)

    def display_pattern(self, index: int, exposure_us: int = 1_000_000,
                        dark_us: int = 0, bitdepth: int = 8, color: int = 7):
        """
        Display cached pattern ``index`` (from load_patterns). Streams the
        pre-encoded image and shows it, but does NOT re-encode or re-enter OTF
        mode, so it's fast.
        """
        if not self._patterns:
            raise DMDError("No patterns loaded; call load_patterns([...]) first.")
        if not 0 <= index < len(self._patterns):
            raise IndexError(f"pattern index {index} out of range "
                             f"(0-{len(self._patterns) - 1})")
        self.stop_pattern()
        self.setup_pattern_LUT_definition(
            pattern_index=0, exposuretime=exposure_us, darktime=dark_us,
            bitdepth=bitdepth, color=color, image_pattern_index=0)
        self.configure_pattern_from_LUT(nr_of_LUT_entries=1, nr_of_patterns_to_display=0)
        for controller, encoded in self._patterns[index]:
            self._send_encoded(0, encoded, controller=controller)
        self.start_pattern()


# Lowercase alias matching the upstream `dlpyc900.dmd` name.
dmd = DMD
