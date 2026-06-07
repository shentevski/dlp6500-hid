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

    Parameters
    ----------
    width, height : int
        Image size in pixels; use the DMD's native resolution.
    radius : float, optional
        Circle radius in pixels. Defaults to 1/4 of the smaller dimension.
    center : (x, y), optional
        Circle center in pixels. Defaults to the image center.
    fg, bg : int
        Foreground (inside circle) and background gray levels, 0-255.

    Returns
    -------
    PIL.Image.Image
        8-bit RGB image with a white circle on a black field.
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
