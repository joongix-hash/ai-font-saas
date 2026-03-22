"""
ui_engine.py  –  UI 9-Slice Generator
---------------------------------------
Splits a UI element image into a 9-slice layout.
  • Auto-detect: finds slice lines by edge contrast
  • Manual: user provides exact pixel offsets
  • Output: sliced_preview.png + metadata.json

9-Slice layout:
  ┌───┬───────┬───┐
  │TL │  TC   │TR │  top-left / top-center / top-right
  ├───┼───────┼───┤
  │ML │  MC   │MR │  mid-left / mid-center / mid-right
  ├───┼───────┼───┤
  │BL │  BC   │BR │  bot-left / bot-center / bot-right
  └───┴───────┴───┘
"""
from __future__ import annotations
import json
import math
from typing import Optional, Tuple
from PIL import Image, ImageDraw


# ─── Auto-detection ───────────────────────────────────────────────────────────
def _horizontal_variance(img: Image.Image, y: int) -> float:
    """Color variance of a single horizontal scan line (RGB)."""
    rgb = img.convert("RGB")
    pixels = [rgb.getpixel((x, y)) for x in range(img.width)]
    if len(pixels) < 2:
        return 0.0
    means = [sum(ch) / len(pixels) for ch in zip(*pixels)]
    return sum(
        sum((c - m) ** 2 for c, m in zip(p, means)) for p in pixels
    ) / len(pixels)


def _vertical_variance(img: Image.Image, x: int) -> float:
    """Color variance of a single vertical scan line (RGB)."""
    rgb = img.convert("RGB")
    pixels = [rgb.getpixel((x, y)) for y in range(img.height)]
    if len(pixels) < 2:
        return 0.0
    means = [sum(ch) / len(pixels) for ch in zip(*pixels)]
    return sum(
        sum((c - m) ** 2 for c, m in zip(p, means)) for p in pixels
    ) / len(pixels)


def _find_slice_lines(img: Image.Image) -> Tuple[int, int, int, int]:
    """
    Auto-detect the 4 slice lines (left, right, top, bottom) by finding
    positions where variance transitions from low→high or high→low.
    Falls back to 1/3 proportions if detection fails.
    """
    w, h = img.size

    def _find_transition(variances: list[float], target_frac: float = 0.25,
                         window: int = 5, start: int = 0, end: int = 0) -> int:
        end = end or len(variances)
        # Find first point where variance rises sharply
        threshold = max(variances) * 0.15 if max(variances) > 0 else 1
        best = int(len(variances) * target_frac)
        for i in range(start + window, end - window):
            if variances[i] > threshold:
                return i
        return best

    # Horizontal slices (top/bottom = y positions)
    h_vars = [_horizontal_variance(img, y) for y in range(h)]
    top_y = _find_transition(h_vars, 0.2, start=1, end=h // 2)
    bot_y_vars = list(reversed(h_vars[h // 2:]))
    bot_y = h - _find_transition(bot_y_vars, 0.2, start=1) - 1

    # Vertical slices (left/right = x positions)
    v_vars = [_vertical_variance(img, x) for x in range(w)]
    left_x = _find_transition(v_vars, 0.2, start=1, end=w // 2)
    right_x_vars = list(reversed(v_vars[w // 2:]))
    right_x = w - _find_transition(right_x_vars, 0.2, start=1) - 1

    # Clamp to valid range
    margin = 4
    top_y  = max(margin, min(top_y, h // 3))
    bot_y  = min(h - margin, max(bot_y, h * 2 // 3))
    left_x = max(margin, min(left_x, w // 3))
    right_x = min(w - margin, max(right_x, w * 2 // 3))

    return left_x, right_x, top_y, bot_y


# ─── Slice & Visualise ────────────────────────────────────────────────────────
def _slice_image(img: Image.Image, lx: int, rx: int, ty: int, by: int) -> dict:
    """
    Returns a dict of the 9 slices as PIL Images.
    Keys: TL, TC, TR, ML, MC, MR, BL, BC, BR
    """
    w, h = img.size
    cols = [(0, lx), (lx, rx), (rx, w)]
    rows = [(0, ty), (ty, by), (by, h)]
    names = [
        ["TL", "TC", "TR"],
        ["ML", "MC", "MR"],
        ["BL", "BC", "BR"],
    ]
    slices = {}
    for ri, (r0, r1) in enumerate(rows):
        for ci, (c0, c1) in enumerate(cols):
            slices[names[ri][ci]] = img.crop((c0, r0, c1, r1))
    return slices


def _build_preview(img: Image.Image, lx: int, rx: int, ty: int, by: int) -> Image.Image:
    """Draw the 9-slice grid lines on a copy of the image."""
    preview = img.convert("RGBA").copy()
    draw = ImageDraw.Draw(preview)
    w, h = preview.size
    line_color = (255, 64, 64, 200)
    dash = 6

    def dashed_h(y: int):
        for x in range(0, w, dash * 2):
            draw.line([(x, y), (min(x + dash, w), y)], fill=line_color, width=2)

    def dashed_v(x: int):
        for y in range(0, h, dash * 2):
            draw.line([(x, y), (x, min(y + dash, h))], fill=line_color, width=2)

    dashed_h(ty)
    dashed_h(by)
    dashed_v(lx)
    dashed_v(rx)

    # Corner labels
    label_color = (255, 64, 64, 255)
    positions = {
        "TL": (2, 2), "TC": (lx + 2, 2), "TR": (rx + 2, 2),
        "ML": (2, ty + 2), "MC": (lx + 2, ty + 2), "MR": (rx + 2, ty + 2),
        "BL": (2, by + 2), "BC": (lx + 2, by + 2), "BR": (rx + 2, by + 2),
    }
    for label, (lbx, lby) in positions.items():
        draw.rectangle([lbx, lby, lbx + 16, lby + 12], fill=(0, 0, 0, 120))
        draw.text((lbx + 1, lby), label, fill=label_color)

    return preview


# ─── Public API ───────────────────────────────────────────────────────────────
def generate_9slice(
    image_path: str,
    *,
    auto_detect: bool = True,
    left: Optional[int] = None,
    right: Optional[int] = None,
    top: Optional[int] = None,
    bottom: Optional[int] = None,
) -> Tuple[Image.Image, Image.Image, dict]:
    """
    Generate 9-slice from image.

    Returns:
        (preview_image, original_image, metadata_dict)
    """
    img = Image.open(image_path).convert("RGBA")
    w, h = img.size

    if auto_detect or any(v is None for v in [left, right, top, bottom]):
        lx, rx, ty, by = _find_slice_lines(img)
    else:
        lx, rx, ty, by = int(left), int(right), int(top), int(bottom)

    # Clamp
    lx = max(1, min(lx, w - 2))
    rx = max(lx + 1, min(rx, w - 1))
    ty = max(1, min(ty, h - 2))
    by = max(ty + 1, min(by, h - 1))

    preview = _build_preview(img, lx, rx, ty, by)
    slices = _slice_image(img, lx, rx, ty, by)

    metadata = {
        "source_size": {"w": w, "h": h},
        "slice_lines": {
            "left": lx,
            "right": rx,
            "top": ty,
            "bottom": by,
        },
        "slices": {
            name: {
                "x": 0, "y": 0,  # placeholder
                "w": slices[name].width,
                "h": slices[name].height,
            }
            for name in slices
        },
        "scale_regions": {
            "horizontal": {"start": lx, "end": rx},
            "vertical": {"start": ty, "end": by},
        },
        "corner_size": {
            "top_left": {"w": lx, "h": ty},
            "top_right": {"w": w - rx, "h": ty},
            "bot_left": {"w": lx, "h": h - by},
            "bot_right": {"w": w - rx, "h": h - by},
        },
        "note": (
            "The 'MC' (center) region tiles horizontally and vertically. "
            "'TC'/'BC' tile horizontally. 'ML'/'MR' tile vertically. "
            "Corner slices (TL/TR/BL/BR) are never scaled."
        ),
    }

    return preview, img, metadata
