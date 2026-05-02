# Mercari Inventory App

A local desktop app that scrapes your active listings from [jp.mercari.com](https://jp.mercari.com), stores them in a SQLite database, and displays them in a browser-based dashboard.

All data stays on your device — no external servers involved.

---

## What it does

| Feature | Detail |
|---|---|
| Sync listings | Opens Chrome, logs into Mercari, scrapes all active listings |
| Dashboard | Flask web UI at `http://127.0.0.1:5000` |
| Smart skip | Skips unchanged items; only fetches detail pages when data changed |
| Parallel fetch | Up to 4 headless Chrome workers fetch detail pages concurrently |
| Jira integration | `create_jira_ticket.py` — optional, requires `.env` credentials |

---

## Prerequisites

| Requirement | Install |
|---|---|
| Python 3.8 – 3.12 | [python.org](https://www.python.org) or `brew install python@3.12` |
| Google Chrome | [chrome.com](https://www.google.com/chrome/) |
| ChromeDriver (matches Chrome version) | `brew install chromedriver` |

After installing ChromeDriver via Homebrew, allow it in macOS:  
**System Settings → Privacy & Security → Security → Allow chromedriver**

---

## How to run locally (development)

```bash
# 1. Clone or copy the project
cd mercari_inventory_app

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the app
python main.py
```

The app will:
1. Print the data directory path
2. Start the Flask server on port 5000
3. Open `http://127.0.0.1:5000` in your browser automatically

To sync Mercari listings, click **同期Mercari商品** in the browser. A Chrome window will open for login — follow the terminal prompt after logging in.

### Optional: Jira integration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

`.env` format:

```
JIRA_URL=https://your-domain.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your-api-token
JIRA_PROJECT_KEY=PROJ
```

---

## How to build the Mac app

> **Requires Python 3.12 or earlier.** PyInstaller's support for Python 3.13+ is still experimental. If your system Python is newer, create a 3.12 venv first (see note in `build_mac.sh`).

```bash
# Make the script executable (first time only)
chmod +x build_mac.sh

# Run the build
./build_mac.sh
```

The build produces:

```
dist/
├── MercariInventory/           ← app bundle (all binaries + deps bundled)
│   ├── MercariInventory        ← Unix executable
│   └── _internal/              ← PyInstaller runtime + bundled packages
└── MercariInventory.command    ← double-click launcher (opens in Terminal)
```

**`MercariInventory.command`** is the file users double-click. macOS opens `.command` files in Terminal.app, which keeps the terminal visible — required for the Mercari login prompt during sync.

### First run after building

```bash
# Or double-click dist/MercariInventory.command in Finder
./dist/MercariInventory/MercariInventory
```

`products.db` will be created inside `dist/MercariInventory/` the first time the app runs.

---

## How to distribute

### 1. Build

```bash
./build_mac.sh
```

### 2. Package

```bash
zip -r MercariInventory.zip dist/
```

### 3. Share

Send `MercariInventory.zip` to the user. They:

1. Unzip it
2. Install ChromeDriver (`brew install chromedriver`) and allow it in Security settings
3. Double-click `MercariInventory.command`

### What's included in the zip

| Included | Not included |
|---|---|
| All Python dependencies (no Python install needed) | `.env` (user creates their own) |
| The app executable | `products.db` (created on first run) |
| `MercariInventory.command` launcher | `venv/` |

> **Note:** The packaged app is macOS-only (arm64 or x86\_64 depending on the machine it was built on). Build on the same architecture as the target machine, or use a universal build if targeting both.

---

## Project structure

```
mercari_inventory_app/
├── main.py               ← entry point: starts Flask + opens browser
├── mercari_sync.py       ← Flask app + Selenium scraper (core logic)
├── app.py                ← lightweight read-only Flask frontend
├── mercari_login.py      ← standalone login/scrape script (legacy)
├── create_jira_ticket.py ← Jira ticket creation utility
├── build_mac.sh          ← PyInstaller build script
├── requirements.txt      ← runtime dependencies
├── .gitignore
└── README.md
```

**User data** (not committed to git):

| File | Description |
|---|---|
| `products.db` | SQLite database — all scraped listings |
| `.env` | Jira API credentials |

---

## Data privacy

This app is entirely local-first:

- No data is sent to any external server by the app itself.
- `products.db` lives on your device.
- Mercari data is fetched directly from Mercari's website using your own session.
- `.env` credentials never leave your machine.
