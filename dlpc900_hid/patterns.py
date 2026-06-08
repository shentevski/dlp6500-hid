"""
Helpers for generating simple pattern images to send to the DMD.

Everything returns an 8-bit RGB Pillow image at the DMD's native resolution
(grayscale content, all three channels equal) so it can be fed straight into
the ERLE encoder / upload path. White (255) = mirror ON, black (0) = mirror OFF.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

# Native mirror-array resolutions for common DLPC900 DMDs (width, height).
RESOLUTIONS = {
    "DLP6500": (1920, 1080),
    "DLP9000": (2560, 1600),
    "DLP670S": (1920, 1080),
    "DLP500YX": (2560, 1600),
    "DLP5500": (1024, 768),
}


def _to_rgb(mask: np.ndarray, fg: int, bg: int) -> Image.Image:
    """Turn a boolean mask (True = foreground) into an 8-bit RGB image."""
    gray = np.where(mask, fg, bg).astype(np.uint8)
    rgb = np.stack([gray, gray, gray], axis=-1)
    return Image.fromarray(rgb, "RGB")


def circle(width: int, height: int,
           radius: float | None = None,
           center: tuple[float, float] | None = None,
           fg: int = 255, bg: int = 0) -> Image.Image:
    """
    Generate a filled circle.

    width, height : image size in pixels (use the DMD's native resolution).
    radius : circle radius in pixels (defaults to 1/4 of the smaller dimension).
    center : (x, y) in pixels (defaults to the image center).
    fg, bg : foreground (inside) and background gray levels, 0-255.
    """
    if center is None:
        center = (width / 2.0, height / 2.0)
    if radius is None:
        radius = min(width, height) / 4.0

    yy, xx = np.ogrid[:height, :width]
    mask = (xx - center[0]) ** 2 + (yy - center[1]) ** 2 <= radius ** 2
    return _to_rgb(mask, fg, bg)


def ring(width: int, height: int,
         outer_radius: float, inner_radius: float,
         center: tuple[float, float] | None = None,
         fg: int = 255, bg: int = 0) -> Image.Image:
    """Generate an annulus (ring) between inner_radius and outer_radius."""
    if center is None:
        center = (width / 2.0, height / 2.0)
    yy, xx = np.ogrid[:height, :width]
    dist2 = (xx - center[0]) ** 2 + (yy - center[1]) ** 2
    mask = (dist2 <= outer_radius ** 2) & (dist2 >= inner_radius ** 2)
    return _to_rgb(mask, fg, bg)


def solid(width: int, height: int, level: int = 255) -> Image.Image:
    """Generate a solid field of a single gray level (255 = all mirrors on)."""
    gray = np.full((height, width), level, dtype=np.uint8)
    return Image.fromarray(np.stack([gray] * 3, axis=-1), "RGB")


# ===========================================================================
# Experiment pattern sets
# ===========================================================================
# Conventions used throughout:
#   * Canvas is the DMD native resolution (1920x1080 for the DLP6500).
#   * Everything is built as a boolean mask of shape (height, width) == (y, x),
#     then turned into a hard-edged binary RGB image (no anti-aliasing).
#   * `on=True`  -> the shape is bright (255) on a dark (0) background.
#     `on=False` -> inverted: the shape is dark (0) on a bright (255) field.
#   * Positions/centers are in pixels; line `position` is the CENTER of the line.


class _PatternSet:
    """Base class: holds the canvas size and the mask -> image conversion."""

    def __init__(self, width: int = 1920, height: int = 1080):
        self.width = width
        self.height = height

    def _render(self, mask: np.ndarray, on: bool = True) -> Image.Image:
        """Turn a bool mask (True = the shape) into a binary RGB image."""
        fg, bg = (255, 0) if on else (0, 255)
        return _to_rgb(mask, fg, bg)          # reuse helper already in this file

    def _blank_mask(self) -> np.ndarray:
        return np.zeros((self.height, self.width), dtype=bool)


class MixWavelengths(_PatternSet):
    """Patterns for the wavelength-mixing experiment: solid fields and 1-3 lines."""

    # `orientation` may be one of these names OR any number = angle in degrees
    # (0 = horizontal, 90 = vertical; measured CCW in image coordinates).
    NAMED_ANGLES = {
        "horizontal": 0.0, "vertical": 90.0,
        "45": 45.0, "diagonal": 45.0,
        "-45": 135.0, "135": 135.0, "antidiagonal": 135.0,
    }

    def _resolve_angle(self, orientation) -> float:
        """Map an orientation (name string or numeric degrees) to degrees."""
        if isinstance(orientation, (int, float)):
            return float(orientation)
        try:
            return self.NAMED_ANGLES[orientation]
        except KeyError:
            raise ValueError(
                f"unknown orientation {orientation!r}; pass a number (degrees) "
                f"or one of {sorted(self.NAMED_ANGLES)}")

    # ---- solid fields -----------------------------------------------------
    def solid_on(self) -> Image.Image:
        """Every mirror ON (full bright field)."""
        return self._render(np.ones((self.height, self.width), dtype=bool), on=True)

    def solid_off(self) -> Image.Image:
        """Every mirror OFF (full dark field)."""
        return self._render(np.ones((self.height, self.width), dtype=bool), on=False)

    # ---- the general N-line engine (private) ------------------------------
    def _lines(self, positions, widths, on: bool = True,
               orientation="vertical") -> Image.Image:
        """
        Draw len(positions) parallel lines at any orientation.

        `orientation` is a name ("vertical", "horizontal", "45", ...) or a
        number = angle in degrees. `positions[i]` is the perpendicular offset
        of line i and `widths[i]` its (perpendicular) thickness, both in pixels.
        For "vertical" the offset is the column x, for "horizontal" the row y;
        for any other angle it is the projection  x*sin(a) + y*cos(a). Pass a
        position of None to center that line. All lines live in one mask, so
        `on` toggles the whole group together.
        """
        positions = list(positions)
        widths = list(widths)
        if len(positions) != len(widths):
            raise ValueError("positions and widths must have the same length")

        angle = np.deg2rad(self._resolve_angle(orientation))
        s, c = np.sin(angle), np.cos(angle)

        # proj[y, x] = perpendicular coordinate of each pixel for this angle.
        yy, xx = np.ogrid[:self.height, :self.width]
        proj = xx * s + yy * c                       # broadcasts to (H, W) float

        # projection of the canvas center, used when a position is None.
        center_proj = ((self.width - 1) / 2.0) * s + ((self.height - 1) / 2.0) * c

        mask = self._blank_mask()
        for pos, w in zip(positions, widths):
            if pos is None:
                pos = center_proj
            mask |= np.abs(proj - pos) <= (w / 2.0)  # OR each line's band in

        return self._render(mask, on)

    def _center_projection(self, orientation) -> float:
        """
        Projection value of the canvas CENTER for this orientation. Adding an
        `offset` (pixels) to it shifts a line `offset` px perpendicular to
        itself from the chip center -- the natural knob for scanning a stripe
        across the field of view (offset=0 -> centered, for any angle).
        """
        angle = np.deg2rad(self._resolve_angle(orientation))
        return ((self.width - 1) / 2.0) * np.sin(angle) + \
               ((self.height - 1) / 2.0) * np.cos(angle)

    # ---- diamond-array safety (empirical) --------------------------------
    # On the DLP6500's diamond mirror grid a thin (~20px) diagonal line lands in
    # a sampling gap and disappears at a few specific offsets. These were
    # measured by sweeping a width-20 line and are CHIP/WIDTH SPECIFIC -- extend
    # the list if you find more, or clear it if you recalibrate.
    diamond_bad_offsets = (-420, -380, -340, -290, 290, 340, 930)
    diamond_clearance = 15      # nudge a requested offset at least this far from a bad one

    def _snap_offset(self, offset: float) -> float:
        """Nudge `offset` just clear of any known diamond-gap offset."""
        o = float(offset)
        for _ in range(10):                      # re-check in case a snap lands near another
            moved = False
            for bad in self.diamond_bad_offsets:
                if abs(o - bad) < self.diamond_clearance:
                    o = bad + self.diamond_clearance if o >= bad else bad - self.diamond_clearance
                    moved = True
            if not moved:
                break
        return o

    # ---- public 1/2/3-line patterns (thin wrappers over _lines) -----------
    # `orientation` is a name ("vertical"/"horizontal"/"45"/...) or degrees.
    # Place a line either absolutely (`position`/`positions` = the perpendicular
    # projection) OR relative to the chip center (`offset`/`offsets`, in pixels;
    # 0 = centered). The center-relative form takes precedence when given.
    # `diamond_safe=True` nudges offsets clear of the known diamond-gap offsets.
    def one_line(self, position=None, width=20, on: bool = True,
                 orientation="vertical", offset=None,
                 diamond_safe: bool = False) -> Image.Image:
        if offset is not None:
            if diamond_safe:
                offset = self._snap_offset(offset)
            position = self._center_projection(orientation) + offset
        # position=None also centers the line on the canvas.
        return self._lines([position], [width], on=on, orientation=orientation)

    def two_lines(self, positions=None, widths=None, on: bool = True,
                  orientation="vertical", offsets=None,
                  diamond_safe: bool = False) -> Image.Image:
        if offsets is not None:
            if diamond_safe:
                offsets = [self._snap_offset(o) for o in offsets]
            cp = self._center_projection(orientation)
            positions = [cp + o for o in offsets]
        if positions is None or widths is None or len(positions) != 2 or len(widths) != 2:
            raise ValueError("two_lines expects 2 positions (or offsets) and 2 widths")
        return self._lines(positions, widths, on=on, orientation=orientation)

    def three_lines(self, positions=None, widths=None, on: bool = True,
                    orientation="vertical", offsets=None,
                    diamond_safe: bool = False) -> Image.Image:
        if offsets is not None:
            if diamond_safe:
                offsets = [self._snap_offset(o) for o in offsets]
            cp = self._center_projection(orientation)
            positions = [cp + o for o in offsets]
        if positions is None or widths is None or len(positions) != 3 or len(widths) != 3:
            raise ValueError("three_lines expects 3 positions (or offsets) and 3 widths")
        return self._lines(positions, widths, on=on, orientation=orientation)


class HBBrush(_PatternSet):
    """Patterns for the HB-brush experiment: a filled disk and a ring."""

    def circle(self, center=None, radius=None, on: bool = True) -> Image.Image:
        """
        Filled disk. `center=(x, y)` defaults to the image center; `radius` in
        pixels defaults to 1/4 of the smaller dimension.
        """
        fg, bg = (255, 0) if on else (0, 255)
        # NOTE: the bare name `circle` here resolves to the module-level
        # circle() function above, NOT this method (methods need `self.`).
        return circle(self.width, self.height, radius=radius, center=center,
                      fg=fg, bg=bg)

    def ring(self, center=None, radius=None, width=None, on: bool = True) -> Image.Image:
        """
        Annulus centered on radius `radius` with thickness `width` (pixels).
        Converts (center-radius, width) -> (outer, inner) for the ring() helper.
        """
        if radius is None:
            radius = min(self.width, self.height) / 4.0
        if width is None:
            width = max(1, int(radius * 0.1))
        outer = radius + width / 2.0
        inner = max(0.0, radius - width / 2.0)
        fg, bg = (255, 0) if on else (0, 255)
        return ring(self.width, self.height, outer_radius=outer,
                    inner_radius=inner, center=center, fg=fg, bg=bg)