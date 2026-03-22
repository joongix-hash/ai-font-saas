"""
sprite_engine.py  –  Sprite Sheet Generator
--------------------------------------------
Packs multiple images into a single sprite atlas.
Supports grid packing and tight (maxrects) packing.
Outputs: sheet.png  +  atlas.json (TexturePacker-compatible)
"""
from __future__ import annotations
import json
import math
from pathlib import Path
from typing import List, Tuple, Optional
from PIL import Image


# ─── Data structures ──────────────────────────────────────────────────────────
class Rect:
    def __init__(self, x: int, y: int, w: int, h: int):
        self.x, self.y, self.w, self.h = x, y, w, h

    def contains(self, other: "Rect") -> bool:
        return (self.x <= other.x and self.y <= other.y and
                self.x + self.w >= other.x + other.w and
                self.y + self.h >= other.y + other.h)

    def area(self) -> int:
        return self.w * self.h


class SpriteFrame:
    def __init__(self, name: str, img: Image.Image):
        self.name = name
        self.img = img
        self.w, self.h = img.size
        self.placed: Optional[Rect] = None


# ─── MaxRects Bin Packing ──────────────────────────────────────────────────────
class MaxRectsBin:
    """Simple BSSF (Best Short-Side Fit) MaxRects packer."""

    def __init__(self, bin_w: int, bin_h: int, padding: int = 0):
        self.bin_w = bin_w
        self.bin_h = bin_h
        self.padding = padding
        self._free: List[Rect] = [Rect(0, 0, bin_w, bin_h)]
        self._used: List[Rect] = []

    def insert(self, w: int, h: int) -> Optional[Rect]:
        pw, ph = w + self.padding, h + self.padding
        best: Optional[Rect] = None
        best_short = float("inf")
        for free in self._free:
            if free.w >= pw and free.h >= ph:
                short = min(free.w - pw, free.h - ph)
                if short < best_short:
                    best_short = short
                    best = Rect(free.x, free.y, w, h)
        if best is None:
            return None
        self._place(best)
        return best

    def _place(self, placed: Rect):
        new_free: List[Rect] = []
        pw = placed.w + self.padding
        ph = placed.h + self.padding
        for free in self._free:
            if not _intersects(free, Rect(placed.x, placed.y, pw, ph)):
                new_free.append(free)
                continue
            # Split
            if placed.x > free.x:
                new_free.append(Rect(free.x, free.y, placed.x - free.x, free.h))
            if placed.x + pw < free.x + free.w:
                new_free.append(Rect(placed.x + pw, free.y,
                                     free.x + free.w - (placed.x + pw), free.h))
            if placed.y > free.y:
                new_free.append(Rect(free.x, free.y, free.w, placed.y - free.y))
            if placed.y + ph < free.y + free.h:
                new_free.append(Rect(free.x, placed.y + ph,
                                     free.w, free.y + free.h - (placed.y + ph)))

        # Remove fully contained rects
        self._free = [r for r in new_free if not any(
            o.contains(r) for o in new_free if o is not r
        )]
        self._used.append(placed)


def _intersects(a: Rect, b: Rect) -> bool:
    return not (a.x + a.w <= b.x or b.x + b.w <= a.x or
                a.y + a.h <= b.y or b.y + b.h <= a.y)


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p <<= 1
    return p


# ─── Public API ───────────────────────────────────────────────────────────────
def generate_sprite_sheet(
    image_paths: List[str],
    *,
    pack_mode: str = "tight",   # "grid" | "tight"
    padding: int = 2,
    cell_w: int = 0,            # 0 = auto (grid only)
    cell_h: int = 0,
    max_sheet_size: int = 4096,
    sort_by: str = "area",      # "area" | "name" | "none"
    pivot_x: float = 0.5,
    pivot_y: float = 0.5,
    bg_color: Tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Tuple[Image.Image, dict]:
    """
    Pack images into a sprite sheet.

    Returns (sheet_image, atlas_dict)
    atlas_dict  is TexturePacker-compatible JSON.
    """
    if not image_paths:
        raise ValueError("이미지를 최소 1개 이상 업로드해주세요.")

    frames: List[SpriteFrame] = []
    for p in image_paths:
        img = Image.open(p).convert("RGBA")
        name = Path(p).stem
        frames.append(SpriteFrame(name, img))

    # Sort
    if sort_by == "area":
        frames.sort(key=lambda f: f.w * f.h, reverse=True)
    elif sort_by == "name":
        frames.sort(key=lambda f: f.name)

    if pack_mode == "grid":
        return _pack_grid(frames, cell_w, cell_h, padding, max_sheet_size,
                          pivot_x, pivot_y, bg_color)
    else:
        return _pack_tight(frames, padding, max_sheet_size,
                           pivot_x, pivot_y, bg_color)


# ─── Grid packing ─────────────────────────────────────────────────────────────
def _pack_grid(
    frames: List[SpriteFrame],
    cell_w: int,
    cell_h: int,
    padding: int,
    max_size: int,
    pivot_x: float,
    pivot_y: float,
    bg_color: tuple,
) -> Tuple[Image.Image, dict]:
    if cell_w <= 0:
        cell_w = max(f.w for f in frames)
    if cell_h <= 0:
        cell_h = max(f.h for f in frames)

    n = len(frames)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    stride_x = cell_w + padding
    stride_y = cell_h + padding
    sheet_w = min(cols * stride_x + padding, max_size)
    sheet_h = min(rows * stride_y + padding, max_size)

    sheet = Image.new("RGBA", (sheet_w, sheet_h), bg_color)
    frames_meta: dict = {}

    for i, frame in enumerate(frames):
        col, row = i % cols, i // cols
        x = padding + col * stride_x
        y = padding + row * stride_y

        # Fit frame inside cell (preserve aspect ratio)
        img = frame.img
        img.thumbnail((cell_w, cell_h), Image.LANCZOS)
        fw, fh = img.size
        # Center in cell
        ox = x + (cell_w - fw) // 2
        oy = y + (cell_h - fh) // 2
        sheet.paste(img, (ox, oy), img)

        frames_meta[frame.name] = {
            "frame": {"x": ox, "y": oy, "w": fw, "h": fh},
            "rotated": False,
            "trimmed": False,
            "spriteSourceSize": {"x": 0, "y": 0, "w": fw, "h": fh},
            "sourceSize": {"w": fw, "h": fh},
            "pivot": {"x": pivot_x, "y": pivot_y},
        }

    atlas = _build_atlas(frames_meta, sheet_w, sheet_h)
    return sheet, atlas


# ─── Tight (MaxRects) packing ─────────────────────────────────────────────────
def _pack_tight(
    frames: List[SpriteFrame],
    padding: int,
    max_size: int,
    pivot_x: float,
    pivot_y: float,
    bg_color: tuple,
) -> Tuple[Image.Image, dict]:
    # Estimate canvas size
    total_area = sum(f.w * f.h for f in frames)
    side = max(256, min(_next_pow2(int(math.sqrt(total_area) * 1.2)), max_size))

    # Try increasing sizes until all frames fit
    for attempt in range(6):
        bin_w = min(side, max_size)
        bin_h = min(side, max_size)
        packer = MaxRectsBin(bin_w, bin_h, padding)
        placed = []
        for frame in frames:
            r = packer.insert(frame.w, frame.h)
            if r is None:
                placed = None
                break
            placed.append((frame, r))

        if placed is not None:
            break
        side = min(side * 2, max_size)
        if side >= max_size and attempt > 0:
            raise ValueError(
                f"이미지가 너무 많거나 큽니다. 최대 {max_size}×{max_size}px를 초과합니다."
            )

    sheet = Image.new("RGBA", (bin_w, bin_h), bg_color)
    frames_meta: dict = {}

    for frame, rect in placed:
        sheet.paste(frame.img.resize((rect.w, rect.h), Image.LANCZOS),
                    (rect.x, rect.y), frame.img.resize((rect.w, rect.h), Image.LANCZOS))
        frames_meta[frame.name] = {
            "frame": {"x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h},
            "rotated": False,
            "trimmed": False,
            "spriteSourceSize": {"x": 0, "y": 0, "w": rect.w, "h": rect.h},
            "sourceSize": {"w": frame.w, "h": frame.h},
            "pivot": {"x": pivot_x, "y": pivot_y},
        }

    atlas = _build_atlas(frames_meta, bin_w, bin_h)
    return sheet, atlas


def _build_atlas(frames_meta: dict, sheet_w: int, sheet_h: int) -> dict:
    return {
        "meta": {
            "app": "CopyFont Sprite Generator",
            "version": "1.0",
            "image": "sheet.png",
            "format": "RGBA8888",
            "size": {"w": sheet_w, "h": sheet_h},
            "scale": "1",
        },
        "frames": frames_meta,
    }
