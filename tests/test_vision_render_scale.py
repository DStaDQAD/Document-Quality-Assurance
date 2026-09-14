"""Tests for the render scale that decides how many image tokens a page costs.

Gemini charges per 768x768 tile: an image is tiled and every tile costs the same 258 tokens, so
the price jumps at each tile boundary rather than with the pixel count. A page rendered at the
old fixed 150 DPI came out 1275x1754 px — 2 tiles wide by 3 tall, 6 tiles — while ANY render
whose long side lands at or under 1536 px is 2x2, four tiles, a third cheaper. Between 96 and
131 DPI the tile count is identical, so the only thing to decide is the highest resolution that
still fits: that is what `render_scale` computes, from the page's own size rather than a DPI
guessed for one paper format.
"""

import math

from pdf_extraction import render_scale

# US Letter-ish page the BI reports use, in PDF points (72 pt = 1 inch).
PAGE_W, PAGE_H = 612.0, 842.0
TILE = 768


def tiles_for(width_px: int, height_px: int) -> int:
    """Gemini's tile count for an image — the unit its image tokens are billed in."""
    if width_px <= 384 and height_px <= 384:
        return 1
    return math.ceil(width_px / TILE) * math.ceil(height_px / TILE)


def rendered_px(scale: float) -> tuple:
    return int(PAGE_W * scale), int(PAGE_H * scale)


def test_report_page_fits_in_four_tiles_instead_of_six():
    """The whole point: one third fewer image tokens for every page sent to a vision model."""
    scale = render_scale(PAGE_W, PAGE_H, dpi=150, max_px=1536)
    w, h = rendered_px(scale)

    assert h <= 1536, f"long side {h}px still crosses the tile boundary"
    assert tiles_for(w, h) == 4
    # The old behaviour, kept here so the saving is visible in the test itself.
    assert tiles_for(*rendered_px(150 / 72)) == 6


def test_uses_the_highest_resolution_that_still_fits():
    """Anything from 96 to 131 DPI costs the same four tiles, so dropping below 131 would
    throw away legibility for free — small digits in a Lampiran table are what gets misread."""
    scale = render_scale(PAGE_W, PAGE_H, dpi=150, max_px=1536)
    effective_dpi = scale * 72

    assert 130 <= effective_dpi <= 131.5
    # One more point of scale would cross back over the boundary.
    assert int(PAGE_H * (effective_dpi + 1) / 72) > 1536


def test_a_small_page_is_never_upscaled_past_the_dpi_cap():
    """max_px is a ceiling, not a target: a page already smaller than it keeps its DPI."""
    scale = render_scale(300.0, 400.0, dpi=150, max_px=1536)

    assert scale == 150 / 72


def test_a_taller_page_still_lands_within_the_ceiling():
    """A3/long-format appendix pages exist; the ceiling is what holds, not an assumed format."""
    scale = render_scale(842.0, 1191.0, dpi=150, max_px=1536)
    w, h = int(842.0 * scale), int(1191.0 * scale)

    assert max(w, h) <= 1536
    assert tiles_for(w, h) == 4


def test_ceiling_can_be_switched_off():
    """Escape hatch for a document that needs the old resolution: max_px=0 means 'just use dpi'."""
    assert render_scale(PAGE_W, PAGE_H, dpi=150, max_px=0) == 150 / 72
