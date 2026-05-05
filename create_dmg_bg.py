"""
Generate dmg_background.png — text-free background for the DMG installer window.

Layout (660 × 400 px):
  Solid neutral background + a single right-pointing arrow centered between
  the app icon drop zone (x≈170) and the Applications folder (x≈490).

No text is rendered — Finder supplies all labels via icon names.

Requires: Pillow
Output:   dmg_background.png  (project root)
"""
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("[dmg-bg] Pillow not found — run: pip install pillow")
    sys.exit(1)

W, H = 660, 400

BG    = "#F5F5F7"   # Apple-style off-white
ARROW = "#B0BEC5"   # neutral gray arrow

# Icon centre positions — must mirror icon_locations in dmgbuild_settings.py
APP_X  = 170
APPS_X = 490
ICON_Y = 175


def _draw_arrow(draw):
    cy    = ICON_Y
    x1    = APP_X  + 80   # clear gap from app icon edge
    x2    = APPS_X - 80   # clear gap from Applications icon edge
    if x2 <= x1:
        return

    shaft_h = 14
    head_w  = 24
    head_h  = 36

    # shaft
    draw.rectangle(
        [(x1, cy - shaft_h // 2), (x2, cy + shaft_h // 2)],
        fill=ARROW,
    )
    # arrowhead
    draw.polygon(
        [
            (x2,          cy - head_h // 2),
            (x2,          cy + head_h // 2),
            (x2 + head_w, cy),
        ],
        fill=ARROW,
    )


def generate_background(output="dmg_background.png"):
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    _draw_arrow(draw)
    img.save(output, "PNG", dpi=(144, 144))
    print(f"[dmg-bg] {output}  ({W}x{H} px, text-free)")


if __name__ == "__main__":
    generate_background("dmg_background.png")
