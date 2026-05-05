"""
Generate dmg_background.png — the background image for the DMG installer window.

Layout (660 x 400 px):
  - Solid light-gray background (#F5F5F7)
  - One right-pointing arrow between the two icon positions
  - No circles, no text — Finder draws its own icon labels

Icon positions must match icon_locations in dmgbuild_settings.py.

Requires: Pillow
Output:   dmg_background.png (project root)
"""
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("[dmg-bg] Pillow not found -- run: pip install pillow")
    sys.exit(1)

W, H = 660, 400

BG    = "#F5F5F7"   # Apple standard light gray
ARROW = "#AAAAAA"   # neutral gray arrow

# Icon centre positions -- must mirror dmgbuild_settings.py icon_locations
APP_X,  APP_Y  = 150, 175
APPS_X, APPS_Y = 510, 175


def _draw_arrow(draw: ImageDraw.ImageDraw, x1: int, x2: int, cy: int) -> None:
    shaft_y1 = cy - 9
    shaft_y2 = cy + 9
    head_tip  = x2 + 28
    draw.rectangle([(x1, shaft_y1), (x2, shaft_y2)], fill=ARROW)
    draw.polygon([(x2, cy - 22), (x2, cy + 22), (head_tip, cy)], fill=ARROW)


def generate_background(output: str = "dmg_background.png") -> None:
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    _draw_arrow(draw, APP_X + 80, APPS_X - 80, APP_Y)

    img.save(output, "PNG")
    print(f"[dmg-bg] OK {output}  ({W}x{H}px)")


if __name__ == "__main__":
    generate_background("dmg_background.png")
