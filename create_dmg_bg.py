"""
Generate dmg_background.png — the background image for the DMG installer window.

Layout (660 × 400 px):
  - Left  (x=170): app icon drop-zone circle
  - Center: right-pointing arrow
  - Right (x=490): Applications folder drop-zone circle

No text is rendered. Finder draws its own file/folder labels beneath each icon.
Icon positions must match icon_locations in dmgbuild_settings.py.

Requires: Pillow
Output:   dmg_background.png (project root)
"""
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("[dmg-bg] Pillow not found — run: pip install pillow")
    sys.exit(1)

W, H = 660, 400

# Colours
BG     = "#F2F2F2"   # light warm gray
CIRCLE = "#E3EEF9"   # very light blue fill for drop-zone circles
RING   = "#7EB5E5"   # blue stroke for drop-zone circles
ARROW  = "#9BBAD8"   # muted blue arrow

# Icon centre positions — must mirror dmgbuild_settings.py icon_locations
APP_X,  APP_Y  = 170, 175
APPS_X, APPS_Y = 490, 175


def _draw_drop_zone(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int = 62) -> None:
    draw.ellipse(
        [(cx - r, cy - r), (cx + r, cy + r)],
        fill=CIRCLE,
        outline=RING,
        width=2,
    )


def _draw_arrow(draw: ImageDraw.ImageDraw, x1: int, x2: int, cy: int) -> None:
    """Solid right-pointing arrow from x1 to x2 at vertical centre cy."""
    shaft_y1 = cy - 9
    shaft_y2 = cy + 9
    head_tip  = x2 + 28
    draw.rectangle([(x1, shaft_y1), (x2, shaft_y2)], fill=ARROW)
    draw.polygon([(x2, cy - 22), (x2, cy + 22), (head_tip, cy)], fill=ARROW)


def generate_background(output: str = "dmg_background.png") -> None:
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    _draw_drop_zone(draw, APP_X,  APP_Y)
    _draw_drop_zone(draw, APPS_X, APPS_Y)
    _draw_arrow(draw, APP_X + 72, APPS_X - 72, APP_Y)

    img.save(output, "PNG")
    print(f"[dmg-bg] ✓ {output}  ({W}×{H}px, text-free)")


if __name__ == "__main__":
    generate_background("dmg_background.png")
