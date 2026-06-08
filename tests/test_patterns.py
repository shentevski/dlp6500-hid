"""
Offline tests for dlpc900_hid.patterns -- no DMD or `hid` needed (numpy+PIL only).

Run directly:    python tests/test_patterns.py
or with pytest:  pytest tests/test_patterns.py

The core guarantees checked: every pattern is the native size, is true RGB
grayscale, and is strictly BINARY (only 0 and 255 -> no anti-aliasing, which
the DMD requires), plus the line geometry/orientation behaves as documented.
"""
import os
import sys

import numpy as np

# Import the patterns module directly (bypass the package __init__, which
# imports `hid` -- unavailable off the instrument PC).
_PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dlpc900_hid")
sys.path.insert(0, _PKG)
import patterns  # noqa: E402

W, H = 1920, 1080


def _gray(img):
    """Return the single channel, asserting native size + RGB + strictly binary."""
    assert img.mode == "RGB", img.mode
    assert img.size == (W, H), img.size
    a = np.asarray(img)
    assert (a[:, :, 0] == a[:, :, 1]).all() and (a[:, :, 1] == a[:, :, 2]).all()
    assert set(np.unique(a).tolist()) <= {0, 255}, sorted(set(np.unique(a).tolist()))
    return a[:, :, 0]


def test_free_functions_binary_and_sized():
    _gray(patterns.solid(W, H, 255))
    _gray(patterns.solid(W, H, 0))
    _gray(patterns.circle(W, H, radius=300))
    _gray(patterns.ring(W, H, 320, 280))


def test_mix_solids():
    mw = patterns.MixWavelengths()
    assert _gray(mw.solid_on()).min() == 255
    assert _gray(mw.solid_off()).max() == 0


def test_vertical_line_position_and_width():
    mw = patterns.MixWavelengths()
    g = _gray(mw.one_line(960, 20, orientation="vertical"))
    cols = np.where(g[H // 2] == 255)[0]
    assert cols.min() == 950 and cols.max() == 970        # centered, width 20
    assert (g[:, 960] == 255).all()                       # full-height column


def test_horizontal_line_position_and_width():
    mw = patterns.MixWavelengths()
    g = _gray(mw.one_line(540, 30, orientation="horizontal"))
    rows = np.where(g[:, W // 2] == 255)[0]
    assert rows.min() == 525 and rows.max() == 555
    assert (g[540, :] == 255).all()                       # full-width row


def test_angles_stay_binary_and_resolve():
    mw = patterns.MixWavelengths()
    for orient in ("45", "diagonal", "-45", "135", 30, 90, 0):
        g = _gray(mw.one_line(None, 40, orientation=orient))
        assert (g == 255).any(), f"nothing drawn for {orient!r}"
    assert mw._resolve_angle("vertical") == 90.0
    assert mw._resolve_angle("horizontal") == 0.0
    assert mw._resolve_angle("45") == 45.0
    assert mw._resolve_angle(30) == 30.0
    try:
        mw._resolve_angle("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("bad orientation should raise ValueError")


def test_multi_line_counts_and_grouping():
    mw = patterns.MixWavelengths()
    g = _gray(mw.two_lines([640, 1280], [10, 10]))
    on_cols = np.where(g[H // 2] == 255)[0]
    assert (np.diff(on_cols) > 1).any(), "expected two separate lines"
    for bad in (lambda: mw.two_lines([1, 2, 3], [1, 2, 3]),
                lambda: mw.three_lines([1, 2], [1, 2])):
        try:
            bad()
        except ValueError:
            pass
        else:
            raise AssertionError("wrong line count should raise ValueError")


def test_invert_flag_swaps_fg_bg():
    mw = patterns.MixWavelengths()
    on = _gray(mw.one_line(960, 20, on=True))
    off = _gray(mw.one_line(960, 20, on=False))
    assert (on == (255 - off)).all()


def test_hbbrush_circle_and_ring():
    hb = patterns.HBBrush()
    c = _gray(hb.circle(center=(960, 540), radius=300, on=True))
    assert c[540, 960] == 255 and c[0, 0] == 0            # center on, corner off
    r = _gray(hb.ring(center=(960, 540), radius=300, width=40))
    assert r[540, 960] == 0                               # hollow center
    assert r[540, 960 + 300] == 255                       # on the ring band


def _run_all():
    import traceback
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
