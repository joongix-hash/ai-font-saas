"""
pixel_engine.py  –  Pixel Art Converter
-----------------------------------------
Converts any image to pixel-art style:
  • Nearest-neighbor downscale → upscale
  • Color palette quantization (8 / 16 / 32 / 64 colors)
  • Optional dithering (Floyd-Steinberg via Pillow)
  • Output as RGBA PNG
"""
from __future__ import annotations
from typing import Tuple, Optional
from PIL import Image


# Allowed output pixel sizes
PIXEL_SIZES = [4, 8, 12, 16, 24, 32, 48, 64]
PALETTE_SIZES = [8, 16, 32, 64]


def convert_to_pixel_art(
    image_path: str,
    *,
    pixel_size: int = 8,           # size of each "pixel block" in output
    output_scale: int = 4,         # upscale factor for the final PNG
    palette_size: int = 16,        # number of colors
    dither: bool = False,          # Floyd-Steinberg dithering
    keep_alpha: bool = True,       # preserve transparency
    outline: bool = False,         # add 1-px dark outline between blocks
    outline_color: Tuple[int, int, int, int] = (0, 0, 0, 255),
) -> Image.Image:
    """
    Convert image → pixel art.
    Returns an RGBA PIL Image ready to save.
    """
    pixel_size = max(1, pixel_size)
    output_scale = max(1, min(output_scale, 8))
    palette_size = max(2, min(palette_size, 256))

    img = Image.open(image_path).convert("RGBA")
    orig_w, orig_h = img.size

    # ── Step 1: Downscale with NEAREST ─────────────────────────────────────
    small_w = max(1, orig_w // pixel_size)
    small_h = max(1, orig_h // pixel_size)
    small = img.resize((small_w, small_h), Image.NEAREST)

    # ── Step 2: Color quantization ─────────────────────────────────────────
    if keep_alpha:
        # Quantize RGB only, preserve alpha mask
        alpha = small.split()[3]
        rgb = small.convert("RGB")
        dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
        rgb_q = rgb.quantize(colors=palette_size, dither=dither_mode).convert("RGB")
        quantized = Image.merge("RGBA", (*rgb_q.split(), alpha))
    else:
        rgb = small.convert("RGB")
        dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
        quantized = rgb.quantize(colors=palette_size, dither=dither_mode).convert("RGBA")

    # ── Step 3: Upscale with NEAREST for crisp blocks ──────────────────────
    out_w = small_w * output_scale
    out_h = small_h * output_scale
    big = quantized.resize((out_w, out_h), Image.NEAREST)

    # ── Step 4: Optional grid outline ──────────────────────────────────────
    if outline and output_scale >= 2:
        big = _add_grid_outline(big, output_scale, outline_color)

    return big


def _add_grid_outline(
    img: Image.Image,
    block_size: int,
    color: Tuple[int, int, int, int],
) -> Image.Image:
    """Draw a 1-px grid between pixel blocks."""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    w, h = img.size
    # Vertical lines
    for x in range(0, w, block_size):
        draw.line([(x, 0), (x, h - 1)], fill=color, width=1)
    # Horizontal lines
    for y in range(0, h, block_size):
        draw.line([(0, y), (w - 1, y)], fill=color, width=1)
    return img


def get_palette_hex(img: Image.Image, max_colors: int = 64) -> list[str]:
    """Extract dominant hex colors from an image."""
    small = img.convert("RGB").resize((100, 100), Image.LANCZOS)
    quantized = small.quantize(colors=max_colors).convert("RGB")
    palette_data = quantized.getcolors(maxcolors=max_colors * 2) or []
    # Sort by frequency desc
    palette_data.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    result = []
    for _, rgb in palette_data:
        h = "#{:02x}{:02x}{:02x}".format(*rgb).upper()
        if h not in seen:
            seen.add(h)
            result.append(h)
    return result[:max_colors]
