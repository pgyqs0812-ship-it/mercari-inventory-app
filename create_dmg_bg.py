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

BG         = "#F5F5F7"          # Apple-style off-white
ARROW_RGB  = (143, 163, 184)    # soft blue-gray; close to macOS system control tint
ARROW_A    = 210                # slight transparency for softer appearance

# Icon centre positions — must mirror icon_locations in dmgbuild_settings.py
APP_X  = 170
APPS_X = 490
ICON_Y = 200


def _draw_arrow(draw):
    cy      = ICON_Y
    icon_r  = 64    # half-width of each icon in the DMG window
    gap     = 16    # clearance between arrow and icon edge

    # Arrowhead tip stops just before the Applications icon left edge.
    tip = APPS_X - icon_r - gap           # 490 - 64 - 16 = 410
    x1  = APP_X  + icon_r + gap           # 170 + 64 + 16 = 250

    # Larger, bolder arrow — more visible and native-feeling
    shaft_h = 22
    head_w  = 40
    head_h  = 54

    x2 = tip - head_w                     # shaft ends at 410 - 40 = 370
    if x2 <= x1:
        return

    color = ARROW_RGB + (ARROW_A,)        # RGBA tuple

    # shaft
    draw.rectangle(
        [(x1, cy - shaft_h // 2), (x2, cy + shaft_h // 2)],
        fill=color,
    )
    # arrowhead — optically nudged 2px right for visual balance
    draw.polygon(
        [
            (x2,       cy - head_h // 2),
            (x2,       cy + head_h // 2),
            (tip + 2,  cy),
        ],
        fill=color,
    )


def generate_background(output="dmg_background.png"):
    # Render in RGBA for soft transparency, then flatten onto BG for RGB PNG output
    bg_rgb = tuple(int(BG.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    img  = Image.new("RGBA", (W, H), bg_rgb + (255,))
    draw = ImageDraw.Draw(img)
    _draw_arrow(draw)
    # Flatten RGBA → RGB (composite over background)
    final = Image.new("RGB", (W, H), bg_rgb)
    final.paste(img, mask=img.split()[3])
    # 72 DPI = 1 pixel per logical point — matches the 660×400 pt window exactly.
    # 144 DPI would cause macOS to treat this as a 2x image covering only
    # 330×200 logical points, rendering the background in the upper-left corner.
    final.save(output, "PNG", dpi=(72, 72))
    print(f"[dmg-bg] {output}  ({W}x{H} px, text-free)")


if __name__ == "__main__":
    generate_background("dmg_background.png")
