"""
Generate AppIcon.icns for the MIA Inventory app.

Design: dark navy rounded-square background, three white/accent horizontal bars
(representing an inventory list), and a small teal sync-indicator circle.
No Mercari logo, no trademark imagery.

Requires: Pillow >= 8.2 (rounded_rectangle), macOS (iconutil)
Output:   AppIcon.icns (project root)
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("[icon] Pillow not found — run: pip install pillow")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
BG_COLOR     = (30,  58,  95, 255)   # #1E3A5F  dark navy
BAR_WHITE    = (255, 255, 255, 255)  # white
BAR_ACCENT   = (160, 200, 235, 255)  # #A0C8EB  muted light-blue (middle bar)
TEAL_CIRCLE  = (38,  198, 218, 255)  # #26C6DA  teal sync dot

# ---------------------------------------------------------------------------
# Icon sizes required by macOS iconset
# ---------------------------------------------------------------------------
ICONSET_SIZES = {
    "icon_16x16.png":       16,
    "icon_16x16@2x.png":    32,
    "icon_32x32.png":       32,
    "icon_32x32@2x.png":    64,
    "icon_128x128.png":     128,
    "icon_128x128@2x.png":  256,
    "icon_256x256.png":     256,
    "icon_256x256@2x.png":  512,
    "icon_512x512.png":     512,
    "icon_512x512@2x.png":  1024,
}

RENDER_SIZE = 1024  # base render canvas; downscaled to each target


# ---------------------------------------------------------------------------
# Draw
# ---------------------------------------------------------------------------

def _draw_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    s = size
    pad    = round(s * 0.08)
    radius = round(s * 0.22)

    # Background: dark navy rounded rectangle
    draw.rounded_rectangle(
        [(pad, pad), (s - pad, s - pad)],
        radius=radius,
        fill=BG_COLOR,
    )

    # ── Three inventory bars (horizontal, centered) ───────────────────────
    bar_h = round(s * 0.072)
    bar_r = bar_h // 2
    bar_specs = [
        (round(s * 0.54), BAR_WHITE),    # top bar:    wide, white
        (round(s * 0.41), BAR_ACCENT),   # middle bar: medium, accent
        (round(s * 0.50), BAR_WHITE),    # bottom bar: wide-medium, white
    ]
    bar_gap    = round(s * 0.032)
    bar_start_y = round(s * 0.345)

    for i, (bar_w, color) in enumerate(bar_specs):
        x1 = (s - bar_w) // 2
        y1 = bar_start_y + i * (bar_h + bar_gap)
        draw.rounded_rectangle(
            [(x1, y1), (x1 + bar_w, y1 + bar_h)],
            radius=bar_r,
            fill=color,
        )

    # ── Teal sync-indicator circle (bottom-right quadrant) ────────────────
    c_r = round(s * 0.100)
    c_x = round(s * 0.720)
    c_y = round(s * 0.665)
    draw.ellipse(
        [(c_x - c_r, c_y - c_r), (c_x + c_r, c_y + c_r)],
        fill=TEAL_CIRCLE,
    )

    # Circular arrow inside the teal circle (open arc + arrowhead)
    arc_r = round(c_r * 0.60)
    arc_w = max(2, round(c_r * 0.17))
    # Arc from 30° to 320° (almost full circle, leaves a gap for arrowhead read)
    draw.arc(
        [c_x - arc_r, c_y - arc_r, c_x + arc_r, c_y + arc_r],
        start=30, end=320,
        fill=BAR_WHITE,
        width=arc_w,
    )
    # Arrowhead at the 320° end of the arc
    a_end    = math.radians(320)
    tip_x    = c_x + arc_r * math.cos(a_end)
    tip_y    = c_y + arc_r * math.sin(a_end)
    tang     = math.radians(320 + 85)   # tangent ≈ 90° ahead of arc end
    ah       = round(c_r * 0.36)
    p1       = (tip_x + ah * math.cos(tang + 0.55), tip_y + ah * math.sin(tang + 0.55))
    p2       = (tip_x + ah * math.cos(tang - 0.55), tip_y + ah * math.sin(tang - 0.55))
    draw.polygon([(tip_x, tip_y), p1, p2], fill=BAR_WHITE)

    return img


# ---------------------------------------------------------------------------
# Build iconset → .icns
# ---------------------------------------------------------------------------

def generate_icns(output_path: str = "AppIcon.icns") -> None:
    if sys.platform != "darwin":
        print("[icon] iconutil requires macOS — skipping .icns generation")
        return

    base_img = _draw_icon(RENDER_SIZE)

    iconset_dir = tempfile.mkdtemp(suffix=".iconset")
    try:
        for filename, target_size in ICONSET_SIZES.items():
            if target_size == RENDER_SIZE:
                icon = base_img.copy()
            else:
                icon = base_img.resize(
                    (target_size, target_size),
                    Image.LANCZOS,
                )
            icon.save(os.path.join(iconset_dir, filename), "PNG")
            print(f"[icon]   {filename} ({target_size}px)")

        result = subprocess.run(
            ["iconutil", "-c", "icns", iconset_dir, "-o", output_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[icon] iconutil failed: {result.stderr}")
            sys.exit(1)

        print(f"[icon] ✓ {output_path}")

    finally:
        shutil.rmtree(iconset_dir, ignore_errors=True)


if __name__ == "__main__":
    generate_icns("AppIcon.icns")
