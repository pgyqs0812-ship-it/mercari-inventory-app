# dmgbuild settings for MIA Inventory DMG installer.
#
# Called by build_mac.sh and the CI "Create DMG" step:
#
#   dmgbuild -s dmgbuild_settings.py \
#     -D app_path="dist/MIAInventory.app" \
#     -D bg_path="dmg_background.png" \
#     "MIA Inventory Installer" \
#     "dist/MIAInventory_Mac_v1.4.x.dmg"
#
# Variables passed via -D:
#   app_path     — path to the .app bundle
#   bg_path      — path to the background PNG (660×400)
import os

# ── Paths ─────────────────────────────────────────────────────────────────────
_app_path     = defines.get("app_path",  "dist/MIAInventory.app")  # noqa: F821
_bg_path      = defines.get("bg_path",   "dmg_background.png")

# Resolve relative paths against cwd (project root, where dmgbuild is invoked).
_cwd = os.getcwd()
_app_path = os.path.abspath(os.path.join(_cwd, _app_path))
_bg_path  = os.path.abspath(os.path.join(_cwd, _bg_path))
_guide    = os.path.abspath(os.path.join(_cwd, "INSTALL.md"))

_appname = os.path.basename(_app_path)   # "MIAInventory.app"

# ── DMG contents ──────────────────────────────────────────────────────────────
files    = [_app_path, _guide]
symlinks = {"Applications": "/Applications"}

# ── Window appearance ─────────────────────────────────────────────────────────
background         = _bg_path
show_status_bar    = False
show_tab_view      = False
show_toolbar       = False
show_pathbar       = False
show_sidebar       = False
sidebar_width      = 180

# Window position (top-left corner) and size (width × height).
# The background image is 660×400 — match exactly.
window_rect        = ((200, 120), (660, 400))

# ── Icon view settings ────────────────────────────────────────────────────────
default_view             = "icon-view"
show_icon_preview        = False
include_icon_view_settings = "auto"
include_list_view_settings = "auto"

arrange_by      = None
grid_offset     = (0, 0)
grid_spacing    = 100
scroll_position = (0, 0)
label_pos       = "bottom"
icon_size       = 128
text_size       = 12

# ── Icon positions ────────────────────────────────────────────────────────────
# (x, y) are the centre coordinates of each icon within the window.
# These must match the visual guides drawn in create_dmg_bg.py.
icon_locations = {
    _appname:       (170, 175),    # app — left drop-zone
    "Applications": (490, 175),    # symlink — right drop-zone
    "INSTALL.md":   (330, 310),    # guide — bottom centre
}
