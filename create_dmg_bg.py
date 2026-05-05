"""
Generate dmg_background.png — the background image for the DMG installer window.

Layout (660 × 400 px):
  - Left  (~x=170): app icon drop-zone with label
  - Center: right-pointing arrow
  - Right (~x=490): Applications shortcut drop-zone with label
  - Bottom: bilingual drag instruction

Icon positions must match icon_locations in dmgbuild_settings.py.

Requires: Pillow
Output:   dmg_background.png (project root)
"""
import os
import subprocess
import sys

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("[dmg-bg] Pillow not found — run: pip install pillow")
    sys.exit(1)

W, H = 660, 400

# ── Colours ──────────────────────────────────────────────────────────────────
BG       = "#F2F2F2"      # light warm gray
CIRCLE   = "#E3EEF9"      # very light blue fill for drop-zone circles
RING     = "#7EB5E5"      # blue stroke for drop-zone circles
ARROW    = "#9BBAD8"      # muted blue arrow
TITLE    = "#2D3748"      # dark text
SUBTITLE = "#4A5568"      # medium text
HINT     = "#718096"      # light text

# Icon center positions — must mirror dmgbuild_settings.py icon_locations
APP_X,   APP_Y   = 170, 175
APPS_X,  APPS_Y  = 490, 175


def _load_font(size: int):
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/SFNSDisplay.ttf",
        "/System/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                from PIL import ImageFont as _F
                return _F.truetype(path, size)
            except Exception:
                continue
    from PIL import ImageFont as _F
    return _F.load_default()


def _centered_text(draw, text, y, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2, y), text, font=font, fill=fill)


def _draw_drop_zone(draw, cx, cy, r=62):
    draw.ellipse(
        [(cx - r, cy - r), (cx + r, cy + r)],
        fill=CIRCLE,
        outline=RING,
        width=2,
    )


def _draw_arrow(draw, x1, x2, cy):
    """Solid right-pointing arrow between x1 and x2 at vertical centre cy."""
    shaft_y1 = cy - 9
    shaft_y2 = cy + 9
    head_x   = x2
    head_tip  = x2 + 28
    # shaft
    draw.rectangle([(x1, shaft_y1), (head_x, shaft_y2)], fill=ARROW)
    # arrowhead (triangle)
    draw.polygon(
        [(head_x, cy - 22), (head_x, cy + 22), (head_tip, cy)],
        fill=ARROW,
    )


def generate_background(output: str = "dmg_background.png") -> None:
    # Resolve version for subtitle
    version = os.environ.get("VERSION", "")
    if not version:
        try:
            version = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            version = ""

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    font_title  = _load_font(22)
    font_label  = _load_font(14)
    font_hint   = _load_font(13)
    font_small  = _load_font(11)

    # ── Drop-zone circles ─────────────────────────────────────────────────
    _draw_drop_zone(draw, APP_X,  APP_Y)
    _draw_drop_zone(draw, APPS_X, APPS_Y)

    # ── Arrow ─────────────────────────────────────────────────────────────
    _draw_arrow(draw, APP_X + 72, APPS_X - 72, APP_Y)

    # ── Labels below circles ──────────────────────────────────────────────
    lbl_y = APP_Y + 72
    for cx, label in [(APP_X, "MIA Inventory"), (APPS_X, "Applications")]:
        bbox = draw.textbbox((0, 0), label, font=font_label)
        lw = bbox[2] - bbox[0]
        draw.text((cx - lw // 2, lbl_y), label, font=font_label, fill=SUBTITLE)

    # ── Title ─────────────────────────────────────────────────────────────
    title = "MIA Inventory Installer"
    if version:
        title += f"  {version}"
    _centered_text(draw, title, 28, font_title, TITLE)

    # ── Instruction ───────────────────────────────────────────────────────
    _centered_text(
        draw,
        "アプリを Applications フォルダへドラッグしてインストール",
        300, font_hint, HINT,
    )
    _centered_text(
        draw,
        "Drag MIA Inventory to the Applications folder to install",
        320, font_small, HINT,
    )

    # ── Disclaimer strip at bottom ────────────────────────────────────────
    draw.rectangle([(0, 364), (W, H)], fill="#E8E8E8")
    _centered_text(
        draw,
        "Independent third-party tool. Not affiliated with Mercari Inc.",
        375, font_small, "#999999",
    )

    img.save(output, "PNG")
    print(f"[dmg-bg] ✓ {output}  ({W}×{H}px)")


if __name__ == "__main__":
    generate_background("dmg_background.png")
