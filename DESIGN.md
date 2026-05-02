# Design Document: Local-First Data Persistence

## Philosophy

MercariInventory is a **local-first** application. All data lives on the user's device.
There is no central server, no cloud sync, no account required beyond the user's own
Mercari credentials. The app uses the network only to scrape the user's own Mercari
listings — never to send data anywhere.

---

## Requirements

### All platforms
1. All synced Mercari product data must be saved to the user's local device.
2. Data must persist across: app close/reopen, device restart, app update.
3. After syncing once, the user must be able to quit, reopen, and search previously
   synced products without syncing again.
4. The app must never require re-sync just to view previously synced data.

### Mac desktop app (current)
- Storage: SQLite database (`products.db`)
- Location: `~/Library/Application Support/MercariInventory/products.db`
  - This is the macOS-standard persistent app data directory
  - Survives app updates (new `dist.zip` extracts do not touch this path)
  - Never inside `dist/MercariInventory/` (next to the executable) — that directory
    is replaced on every update
- Directory created automatically on first launch if it does not exist
- One-time migration: if the new location has no DB but the old location
  (`dist/MercariInventory/products.db`) exists, copy it automatically

### iOS app (future)
- Storage: SQLite or Core Data, stored in the app's sandboxed Documents directory
- No central server; no iCloud sync required (local-only is acceptable)
- Data shared between app sessions via the same local file

---

## Current Implementation (Mac)

### Data directory resolution (`main.py`)

```python
def get_data_dir() -> str:
    if getattr(sys, "frozen", False):
        # PyInstaller frozen binary → ~/Library/Application Support/MercariInventory/
        app_support = os.path.expanduser("~/Library/Application Support")
        data_dir = os.path.join(app_support, "MercariInventory")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    # Development (plain Python) → project root
    return os.path.dirname(os.path.abspath(__file__))
```

### DB path (absolute, no CWD dependency)

`mercari_sync.DB_NAME` is patched to an absolute path in `main.py` before the module
is used, so the database is always found regardless of the process working directory:

```python
import mercari_sync
mercari_sync.DB_NAME = os.path.join(data_dir, "products.db")
```

### One-time migration

On first launch after an update, if `~/Library/Application Support/MercariInventory/products.db`
does not exist but `<executable_dir>/products.db` does, the old file is copied to the
new location so existing sync data is not lost.

---

## Acceptance Criteria

| Step | Expected result |
|---|---|
| Run the app, click Sync | Products appear in the table |
| Quit the app (click 終了 or Ctrl+C) | App exits |
| Reopen the app | App starts without syncing |
| Search / filter products | Previously synced products are visible |
| Download a new release zip, extract, run | Previously synced products are still visible |

---

## Database Schema

Table: `mercari_products`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `item_url` | TEXT UNIQUE | Mercari item URL (stable ID) |
| `title` | TEXT | Product name |
| `price` | TEXT | Display price (e.g. "¥1,000") |
| `status` | TEXT | 出品中 / 取引中 / 売却済み / 販売履歴 |
| `visibility_status` | TEXT | `public` / `stopped` (for 公開停止中 sub-state) |
| `created_at` | TEXT | Listing creation date (JST) |
| `synced_at` | TEXT | Last sync timestamp (JST) |
| `raw_text` | TEXT | Raw scraped text blob |

---

## Non-Goals

- Cloud backup or sync between devices
- Multi-user access
- Remote database
- Automatic background sync (sync is always user-initiated)
