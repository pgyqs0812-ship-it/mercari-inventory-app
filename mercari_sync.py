import json
import os
import re
import queue
import threading
import time
import webbrowser
import sqlite3
import html as html_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

try:
    from _version import __version__ as APP_VERSION
except ImportError:
    APP_VERSION = "dev"

import io
import csv

from flask import Flask, redirect, request, Response
import openpyxl
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

DB_NAME          = "products.db"
COOKIE_FILE      = "mercari_session.json"  # patched to absolute path by main.py
CHROME_PROFILE_DIR = ""                    # patched to absolute path by main.py
LICENSE_FILE     = "license.json"          # patched to absolute path by main.py


class _SyncStopped(Exception):
    """Raised inside run_scraper() / scraper loops when force-stop is requested."""
MAX_WORKERS = 4
MAX_RETRY   = 3

# Monetization constants
_TRIAL_DAYS      = 3
_MONTHLY_DAYS    = 30
_FREE_SYNC_LIMIT = 3    # max syncs per day on free plan
_FREE_ITEM_LIMIT = 100  # max stored items on free plan
# v2 secret — keys now use MIA-LIFE-XXXX-XXXX / MIA-MONTH-XXXX-XXXX format.
# Test keys: MIA-MONTH-7A33-AD8C  |  MIA-LIFE-7114-3540
_LICENSE_SECRET = b"mia-license-v2-secret"
_license_cache: dict = {}   # in-memory; cleared on every write to license.json

_JST = timezone(timedelta(hours=9))

# Statuses with larger listing counts that need a longer pagination timeout
_LONG_TIMEOUT_STATUSES = {"売却済み", "販売履歴"}

# Populated at the end of each sync run; read by home() to show the popup
_last_sync_summary: dict = {}

# Guard against concurrent sync requests
_sync_running = False
_sync_stop_requested = False

# Login session state machine:
#   "unknown" → "checking" → "found_session" / "invalid"
#   "found_session" → "valid" / "logging_in" / "clearing" → "invalid"
#   "logging_in" → "valid" / "invalid"
_session_state: str = "unknown"
_session_last_login: str = ""  # "YYYY-MM-DD HH:MM" — set on save or check

# Live progress state updated by the background sync thread; read by /sync_status
_sync_progress: dict = {
    "running":     False,
    "done":        True,   # True at startup so UI doesn't show stale "in-progress"
    "step":        "",
    "step_num":    0,
    "total_steps": 0,
    "fetched":     0,
    "error":       "",
    "stopped":     False,
}

# Singleton visible-Chrome driver shared by sync and link-open
_singleton_driver = None
_driver_lock = threading.Lock()   # guards singleton creation only


def jst_now() -> str:
    """Return the current time in JST as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now(tz=_JST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Session / cookie helpers
# ---------------------------------------------------------------------------

def _save_session_cookies(driver) -> None:
    """Persist Mercari session cookies to COOKIE_FILE for later re-use."""
    global _session_last_login
    if not COOKIE_FILE:
        return
    try:
        cookies = driver.get_cookies()
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False)
        _session_last_login = jst_now()[:16]
        print(f"[session] クッキーを保存しました ({len(cookies)} 件): {COOKIE_FILE}")
    except Exception as e:
        print(f"[session] クッキー保存失敗: {e}")


def _inject_saved_cookies(driver) -> bool:
    """Load cookies from COOKIE_FILE and inject them into driver.

    The driver must already have navigated to jp.mercari.com (or any page on
    the domain) before cookies can be added; this function handles that
    navigation internally.
    Returns True if cookies were loaded from disk.
    """
    if not COOKIE_FILE or not os.path.exists(COOKIE_FILE):
        return False
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception:
        return False
    if not cookies:
        return False

    driver.get("https://jp.mercari.com")
    time.sleep(0.8)
    safe_keys = {"name", "value", "domain", "path", "secure", "httpOnly"}
    for cookie in cookies:
        try:
            driver.add_cookie({k: v for k, v in cookie.items() if k in safe_keys})
        except Exception:
            pass
    return True


def _try_restore_session(driver) -> bool:
    """Verify the driver has a valid Mercari session, restoring it if needed.

    Strategy 1 — direct navigation: works when the persistent Chrome profile
    (--user-data-dir) already holds valid session cookies.
    Strategy 2 — JSON injection: fallback for a clean/new profile or when the
    profile cookies have expired but the JSON backup is still valid.
    """
    # Strategy 1: profile cookies (the normal case after first login)
    driver.get("https://jp.mercari.com/mypage/listings")
    time.sleep(2)
    if "login" not in driver.current_url:
        print("[session] セッション有効（プロファイルのCookie）— ログイン不要")
        _save_session_cookies(driver)
        return True

    # Strategy 2: inject from JSON backup file
    if _inject_saved_cookies(driver):
        driver.get("https://jp.mercari.com/mypage/listings")
        time.sleep(2)
        if "login" not in driver.current_url:
            print("[session] セッション復元（JSON Cookie）— ログイン不要")
            _save_session_cookies(driver)
            return True

    print("[session] セッション無効 → ログインが必要です")
    return False


# ---------------------------------------------------------------------------
# Singleton driver
# ---------------------------------------------------------------------------

def _is_driver_alive(driver) -> bool:
    """Return True if the driver's Chrome process is still responsive (3 s timeout)."""
    result = [False]

    def _check():
        try:
            _ = driver.current_url
            result[0] = True
        except Exception:
            pass

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout=3.0)
    return result[0]


def _kill_orphan_chromedriver() -> None:
    """Kill stale chromedriver processes left over from a previous crashed session."""
    try:
        import subprocess
        subprocess.run(["pkill", "-f", "chromedriver"], capture_output=True, timeout=5)
        time.sleep(0.3)
    except Exception:
        pass


def _ensure_selenium_manager() -> None:
    """Locate the Selenium Manager binary and ensure it is executable.

    PyInstaller's collect_data_files() does not preserve the +x bit on
    binary files. This function fixes that at runtime and pins SE_MANAGER_PATH
    so Selenium always finds the correct binary regardless of how path
    resolution behaves inside the frozen bundle.
    """
    if os.environ.get("SE_MANAGER_PATH"):
        return  # already pinned by a previous call

    import selenium as _sel
    selenium_pkg_dir = os.path.dirname(os.path.abspath(_sel.__file__))
    sm_path = os.path.join(
        selenium_pkg_dir, "webdriver", "common", "macos", "selenium-manager"
    )

    if not os.path.isfile(sm_path):
        print(f"[driver] selenium-manager が見つかりません: {sm_path}")
        return

    if not os.access(sm_path, os.X_OK):
        try:
            os.chmod(sm_path, 0o755)
            print(f"[driver] selenium-manager の実行権限を付与しました: {sm_path}")
        except OSError as exc:
            print(f"[driver] selenium-manager chmod 失敗: {exc}")

    os.environ["SE_MANAGER_PATH"] = sm_path
    print(f"[driver] SE_MANAGER_PATH={sm_path}")


def _get_or_create_driver() -> "webdriver.Chrome":
    """Return the long-lived singleton visible Chrome driver.

    Reuses the existing driver when it is still alive. If it is dead (user
    closed the Chrome window, or a transient error), the old driver is quit
    gracefully, orphan processes and stale profile lock files are cleaned up,
    and a new driver is created.
    Profile locks are always cleared before creating a new driver, even on
    a fresh process start where _singleton_driver was never set.
    """
    global _singleton_driver
    with _driver_lock:
        if _singleton_driver is not None and not _is_driver_alive(_singleton_driver):
            print("[driver] Driver無効 — 再生成します")
            try:
                _singleton_driver.quit()
            except Exception:
                pass
            _singleton_driver = None
            time.sleep(0.5)          # let Chrome release file handles
            _kill_orphan_chromedriver()
            _clear_profile_lock()    # remove any stale lock files

        if _singleton_driver is None:
            # Always clear profile locks before creating a new driver so stale
            # locks from a previous app session (or crashed Chrome) don't block startup.
            _clear_profile_lock()
            _singleton_driver = _make_chrome_driver(headless=False)

        return _singleton_driver


app = Flask(__name__)

TIME_KEYWORDS = [
    "秒前", "分前", "時間前", "日前",
    "ヶ月前", "か月前", "年前",
    "半年前", "半年以上前",
]

INVALID_TITLES = {
    "公開停止中", "出品停止中", "売却済み", "出品中", "取引中", "名称未取得", "販売履歴"
}

# Statuses that have dedicated scraping URLs (sync targets)
STATUSES = ["出品中", "取引中", "売却済み", "販売履歴"]

# Statuses shown in the search filter UI.
# 販売履歴 is intentionally excluded — it is a sync target and DB value but
# not useful as a search filter in normal usage.
# 公開停止中 is a sub-filter of 出品中 (stored as visibility_status='stopped').
FILTER_STATUSES = ["出品中", "公開停止中", "取引中", "売却済み"]

# Badge texts that can appear on cards (superset of STATUSES for detection)
_DETECT_STATUSES = set(STATUSES) | {"公開停止中", "出品停止中"}

# Mercari mypage URL for each sync status
STATUS_URLS = {
    "出品中":   "https://jp.mercari.com/mypage/listings",
    "取引中":   "https://jp.mercari.com/mypage/listings/in_progress",
    "売却済み": "https://jp.mercari.com/mypage/listings/completed",
    "販売履歴": "https://jp.mercari.com/mypage/listings/sold",
}


# ---------------------------------------------------------------------------
# License / monetization helpers
# ---------------------------------------------------------------------------

import hashlib as _hashlib
import hmac as _hmac_mod


def _get_license() -> dict:
    """Read license.json into _license_cache (or return cached value)."""
    global _license_cache
    if _license_cache:
        return _license_cache
    try:
        if LICENSE_FILE and os.path.exists(LICENSE_FILE):
            with open(LICENSE_FILE, "r", encoding="utf-8") as f:
                _license_cache = json.load(f)
    except Exception:
        _license_cache = {}
    return _license_cache


def _save_license(state: dict) -> None:
    """Write license.json and refresh in-memory cache."""
    global _license_cache
    _license_cache = {}          # clear first so next _get_license re-reads
    _license_cache = state.copy()
    if LICENSE_FILE:
        try:
            with open(LICENSE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[license] 保存失敗: {exc}")


def init_license() -> None:
    """Create license.json on first launch (records trial start).
    Called once from main.py after paths are patched."""
    global _license_cache
    _license_cache = {}          # force re-read from (possibly newly patched) path
    state = _get_license()
    if not state.get("first_launch"):
        state = {
            "license_schema_version": 2,
            "first_launch":    datetime.now(_JST).isoformat(),
            "plan":            "trial",
            "activated_at":    None,
            "expiry_time":     None,   # set when monthly key is activated
            "last_sync_date":  None,
            "validation_server": None, # future: URL for online key verification
        }
        _save_license(state)
        print(f"[license] トライアル開始: {state['first_launch']}")
    else:
        # Migrate older license.json to schema v2 if fields are missing
        changed = False
        for field, default in (
            ("license_schema_version", 2),
            ("expiry_time",     None),
            ("validation_server", None),
            ("sync_count",      0),
        ):
            if field not in state:
                state[field] = default
                changed = True
        if changed:
            _save_license(state)
            print("[license] schema v2 へ移行しました")
        plan = _check_plan()
        days = _trial_days_remaining()
        print(f"[license] プラン: {plan}, トライアル残り: {days} 日")


def _check_plan() -> str:
    """Return the effective plan: trial | expired | free | monthly | lifetime."""
    state = _get_license()
    plan  = state.get("plan", "trial")
    if plan == "trial":
        first = state.get("first_launch")
        if first:
            try:
                launched = datetime.fromisoformat(first)
                if (datetime.now(_JST) - launched).days >= _TRIAL_DAYS:
                    return "expired"
            except Exception:
                pass
    elif plan == "monthly":
        expiry = state.get("expiry_time")
        if expiry:
            try:
                if datetime.now(_JST) > datetime.fromisoformat(expiry):
                    return "free"   # monthly period ended → revert to free
            except Exception:
                pass
    return plan


def _trial_days_remaining() -> int:
    """Return days left in the trial (0 if expired or not in trial)."""
    state = _get_license()
    if state.get("plan") not in ("trial", None):
        return 0
    first = state.get("first_launch")
    if not first:
        return _TRIAL_DAYS
    try:
        launched = datetime.fromisoformat(first)
        elapsed  = (datetime.now(_JST) - launched).days
        return max(0, _TRIAL_DAYS - elapsed)
    except Exception:
        return 0


def _monthly_days_remaining() -> int:
    """Days until monthly plan expires (0 when expired or not monthly)."""
    if _check_plan() != "monthly":
        return 0
    expiry = _get_license().get("expiry_time")
    if not expiry:
        return _MONTHLY_DAYS
    try:
        delta = datetime.fromisoformat(expiry) - datetime.now(_JST)
        return max(0, delta.days)
    except Exception:
        return 0


def _monthly_expiry_str() -> str:
    """Return the monthly expiry date as 'YYYY-MM-DD', or '' if unavailable."""
    expiry = _get_license().get("expiry_time")
    if not expiry:
        return ""
    try:
        return datetime.fromisoformat(expiry).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _is_paid() -> bool:
    return _check_plan() in ("monthly", "lifetime")


def _validate_license_key(key: str) -> tuple:
    """Validate a license key and return (plan_str, error_str).

    Key format:  MIA-LIFE-XXXX-XXXX   (lifetime plan)
                 MIA-MONTH-XXXX-XXXX  (monthly plan)

    XXXX-XXXX = first 8 hex chars of HMAC-SHA256(_LICENSE_SECRET, tag.lower())
                split into two groups of 4 (e.g. "7114-3540").

    This is a local offline check — no network call required.
    Future: pass key to validation server; treat server 200 as authoritative.
    """
    key = key.strip().upper()
    for tag, plan_name in (("LIFE", "lifetime"), ("MONTH", "monthly")):
        raw_hex = _hmac_mod.new(
            _LICENSE_SECRET,
            tag.lower().encode(),
            _hashlib.sha256,
        ).hexdigest()[:8].upper()
        expected_key = f"MIA-{tag}-{raw_hex[:4]}-{raw_hex[4:]}"
        if key == expected_key:
            return plan_name, ""
    return "", "無効なライセンスキーです。キーの形式は MIA-LIFE-XXXX-XXXX または MIA-MONTH-XXXX-XXXX です。"


def _license_badge_html() -> str:
    """Return an HTML pill showing current plan status for the Main page."""
    plan = _check_plan()
    if plan == "trial":
        days = _trial_days_remaining()
        bg, fg = "#fef3c7", "#92400e"
        label  = f"トライアル: 残り {days} 日"
    elif plan == "expired":
        bg, fg = "#fee2e2", "#991b1b"
        label  = "トライアル終了 — アップグレードが必要です"
    elif plan == "free":
        bg, fg = "#f3f4f6", "#374151"
        label  = "無料版"
    elif plan == "monthly":
        bg, fg = "#dcfce7", "#166534"
        days = _monthly_days_remaining()
        exp  = _monthly_expiry_str()
        label = f"Pro 月額 — 残り {days} 日 ({exp} まで)" if exp else "Pro 月額プラン"
    elif plan == "lifetime":
        bg, fg = "#dcfce7", "#166534"
        label  = "Pro 買い切り（無期限）"
    else:
        bg, fg = "#f3f4f6", "#374151"
        label  = plan
    return (
        f'<span class="badge" style="background:{bg};color:{fg};'
        f'font-size:13px;padding:5px 14px;border-radius:20px">'
        f'{html_module.escape(label)}</span>'
    )


def _get_daily_sync_count() -> int:
    """Return today's completed sync count for free plan (resets at midnight JST)."""
    state = _get_license()
    today = datetime.now(_JST).strftime("%Y-%m-%d")
    if state.get("last_sync_date") != today:
        return 0
    return int(state.get("sync_count", 0))


def _increment_sync_count() -> None:
    """Record a completed sync for today (free-plan daily limit tracking)."""
    state = _get_license()
    today = datetime.now(_JST).strftime("%Y-%m-%d")
    if state.get("last_sync_date") != today:
        state["last_sync_date"] = today
        state["sync_count"] = 1
    else:
        state["sync_count"] = int(state.get("sync_count", 0)) + 1
    _save_license(state)


def _enforce_free_item_limit() -> int:
    """Delete items beyond _FREE_ITEM_LIMIT (oldest first). Returns count deleted."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM mercari_products")
        total = cursor.fetchone()[0]
        if total > _FREE_ITEM_LIMIT:
            cursor.execute(
                "DELETE FROM mercari_products WHERE id NOT IN "
                "(SELECT id FROM mercari_products ORDER BY id DESC LIMIT ?)",
                (_FREE_ITEM_LIMIT,)
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
            print(f"[license] 無料プラン: 古い {deleted} 件を削除しました (上限 {_FREE_ITEM_LIMIT} 件)")
            return deleted
        conn.close()
    except Exception as exc:
        print(f"[license] item limit 適用エラー: {exc}")
    return 0


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mercari_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_url TEXT UNIQUE,
            title TEXT,
            price TEXT,
            created_at TEXT,
            raw_text TEXT,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("PRAGMA table_info(mercari_products)")
    columns = [col[1] for col in cursor.fetchall()]
    if "created_at" not in columns:
        cursor.execute("ALTER TABLE mercari_products ADD COLUMN created_at TEXT")
    if "status" not in columns:
        cursor.execute("ALTER TABLE mercari_products ADD COLUMN status TEXT DEFAULT ''")
    # KAN-11: track 公開停止中 as a sub-state of 出品中
    if "visibility_status" not in columns:
        cursor.execute("ALTER TABLE mercari_products ADD COLUMN visibility_status TEXT DEFAULT ''")
    # Backfill: records without status treated as 出品中
    cursor.execute(
        "UPDATE mercari_products SET status = '出品中' WHERE status IS NULL OR status = ''"
    )
    # KAN-10: correct status names to match URL mapping (order-safe)
    cursor.execute("UPDATE mercari_products SET status = '販売履歴' WHERE status = '売却済み'")
    cursor.execute("UPDATE mercari_products SET status = '売却済み' WHERE status = '公開停止中'")
    conn.commit()

    # Log current DB state at startup so counts are visible in the terminal
    cursor.execute(
        "SELECT status, COUNT(*) FROM mercari_products GROUP BY status ORDER BY COUNT(*) DESC"
    )
    rows = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) FROM mercari_products")
    total = cursor.fetchone()[0]
    conn.close()

    if total:
        print("[DB] 起動時レコード数:")
        for s, c in rows:
            print(f"  {s}: {c}")
        print(f"  合計: {total}")
    else:
        print("[DB] データなし（初回起動）")


# Badge style per status: (bg_color, text_color)
STATUS_BADGE = {
    "出品中":    ("#dcfce7", "#166534"),
    "公開停止中": ("#fee2e2", "#991b1b"),
    "取引中":    ("#fef9c3", "#854d0e"),
    "売却済み":  ("#f3f4f6", "#374151"),
    "販売履歴":  ("#dbeafe", "#1e40af"),
}


def _query_products(q="", statuses=None):
    """Query mercari_products filtered by keyword and status list.

    公開停止中 is handled as a sub-filter: status='出品中' AND visibility_status='stopped'.
    売却済み implicitly covers 販売履歴 — the KAN-10 DB migration renamed the
    original 売却済み records to 販売履歴, so both values represent "sold" items.
    Multiple selected statuses are OR-combined.
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = ("SELECT title, price, item_url, created_at, synced_at, status, visibility_status "
           "FROM mercari_products WHERE 1=1")
    params = []
    if q:
        sql += " AND title LIKE ?"
        params.append(f"%{q}%")
    if statuses:
        # Expand 売却済み to also cover 販売履歴 records (same logical category)
        expanded = list(statuses)
        if "売却済み" in expanded and "販売履歴" not in expanded:
            expanded.append("販売履歴")
        regular = [s for s in expanded if s != "公開停止中"]
        include_stopped = "公開停止中" in expanded
        conditions = []
        if regular:
            ph = ",".join("?" * len(regular))
            conditions.append(f"status IN ({ph})")
            params.extend(regular)
        if include_stopped:
            conditions.append("(status = '出品中' AND visibility_status = 'stopped')")
        if conditions:
            sql += " AND (" + " OR ".join(conditions) + ")"
    sql += " ORDER BY id DESC"
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Flask UI
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #f0f2f5;
  --surface: #ffffff;
  --border: #e5e7eb;
  --text: #111827;
  --muted: #6b7280;
  --primary: #2563eb;
  --primary-h: #1d4ed8;
  --radius: 12px;
  --shadow: 0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.05);
  --shadow-md: 0 4px 6px rgba(0,0,0,.07), 0 2px 4px rgba(0,0,0,.05);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--text); min-height: 100vh; }
header { background: #111827; color: #fff; padding: 0; }
.header-inner { max-width: 1160px; margin: 0 auto;
                padding: 20px 24px; display: flex;
                justify-content: space-between; align-items: center; }
.header-inner h1 { font-size: 22px; font-weight: 700; letter-spacing: -.3px; }
.header-inner p  { font-size: 13px; color: #9ca3af; margin-top: 2px; }
.db-pill { background: rgba(255,255,255,.12); border-radius: 20px;
           padding: 5px 14px; font-size: 13px; color: #d1d5db; white-space: nowrap; }
.btn-exit { background: #ef4444; color: #fff; border: none; border-radius: 8px;
            padding: 7px 14px; font-size: 13px; font-weight: 500; cursor: pointer;
            transition: background .15s; white-space: nowrap; }
.btn-exit:hover { background: #dc2626; }
main { max-width: 1160px; margin: 0 auto; padding: 28px 24px; display: flex;
       flex-direction: column; gap: 20px; }
.card { background: var(--surface); border-radius: var(--radius);
        box-shadow: var(--shadow-md); overflow: clip; }
.card-header { display: flex; justify-content: space-between; align-items: center;
               padding: 16px 20px; border-bottom: 1px solid var(--border); }
.card-title { font-size: 14px; font-weight: 600; color: var(--text); }
.card-body  { padding: 20px; }
.field-label { font-size: 12px; font-weight: 600; color: var(--muted);
               text-transform: uppercase; letter-spacing: .05em; margin-bottom: 10px; }
.cb-row { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.cb-label { display: flex; align-items: center; gap: 7px; font-size: 14px;
            cursor: pointer; user-select: none;
            background: #f9fafb; border: 1px solid var(--border);
            border-radius: 8px; padding: 7px 14px; transition: border-color .15s; }
.cb-label:hover { border-color: var(--primary); }
.cb-label input { width: 15px; height: 15px; cursor: pointer; accent-color: var(--primary); }
.search-row { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; }
.text-input { flex: 1; border: 1px solid var(--border); border-radius: 8px;
              padding: 9px 14px; font-size: 14px; outline: none;
              transition: border-color .15s; }
.text-input:focus { border-color: var(--primary);
                    box-shadow: 0 0 0 3px rgba(37,99,235,.1); }
.btn { display: inline-flex; align-items: center; gap: 6px; border: none;
       border-radius: 8px; padding: 9px 18px; font-size: 14px;
       font-weight: 500; cursor: pointer; text-decoration: none;
       transition: background .15s, opacity .15s; white-space: nowrap; }
.btn-primary { background: var(--primary); color: #fff; }
.btn-primary:hover { background: var(--primary-h); }
.btn-outline { background: #fff; color: var(--text);
               border: 1px solid var(--border); }
.btn-outline:hover { background: #f9fafb; }
.btn:disabled, .btn[disabled] { opacity: .4; cursor: not-allowed; pointer-events: none; }
.export-row { display: flex; gap: 8px; align-items: center; }
.count-badge { display: inline-block; background: #eff6ff; color: #1d4ed8;
               border-radius: 20px; padding: 2px 10px; font-size: 12px;
               font-weight: 600; margin-left: 8px; }
table { width: 100%; border-collapse: separate; border-spacing: 0; }
/* Sticky search/filter card */
#search-card { position: sticky; top: 8px; z-index: 10; }
/* Sticky table header — JS sets correct top offset to sit below #search-card */
thead th { background: #f9fafb; color: var(--muted); font-size: 12px;
           font-weight: 600; text-transform: uppercase; letter-spacing: .05em;
           padding: 10px 14px; text-align: left;
           border-bottom: 2px solid var(--border);
           position: sticky; top: 0; z-index: 5; }
tbody td { padding: 11px 14px; border-bottom: 1px solid var(--border);
           font-size: 14px; vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: #f9fafb; }
.price { font-weight: 600; color: #dc2626; white-space: nowrap; }
.badge { display: inline-block; border-radius: 20px;
         padding: 3px 10px; font-size: 12px; font-weight: 600;
         white-space: nowrap; }
.link-btn { color: var(--primary); font-weight: 600; text-decoration: none;
            font-size: 13px; }
.link-btn:hover { text-decoration: underline; }
.empty-state { text-align: center; padding: 56px 20px; color: var(--muted); }
.empty-state .es-icon { font-size: 40px; margin-bottom: 12px; }
.empty-state p { font-size: 15px; }
/* Sync summary modal */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.5);
                 display: flex; align-items: center; justify-content: center;
                 z-index: 1000; }
.modal-box { background: #fff; border-radius: 16px; padding: 32px 36px;
             min-width: 340px; max-width: 480px; box-shadow: 0 20px 40px rgba(0,0,0,.2); }
.modal-box h2 { font-size: 18px; font-weight: 700; margin-bottom: 20px; color: #111827; }
.modal-table { width: 100%; border-collapse: collapse; margin-bottom: 20px; }
.modal-table td { padding: 6px 0; font-size: 14px; border-bottom: 1px solid #f3f4f6; }
.modal-table td:first-child { color: #6b7280; width: 55%; }
.modal-table td:last-child { font-weight: 600; text-align: right; }
.modal-close { width: 100%; padding: 10px; background: #2563eb; color: #fff;
               border: none; border-radius: 8px; font-size: 14px; font-weight: 600;
               cursor: pointer; transition: background .15s; }
.modal-close:hover { background: #1d4ed8; }
/* Sortable column headers */
thead th[data-sortable] { cursor: pointer; user-select: none; white-space: nowrap; }
thead th[data-sortable]:hover { background: #f1f5f9; }
thead th[data-sortable]:hover .sort-icon { color: var(--primary); }
.sort-icon { font-size: 10px; color: #d1d5db; margin-left: 4px; }
/* Error banner */
.error-banner { background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px;
                color: #dc2626; padding: 14px 18px; font-size: 14px; line-height: 1.5; }
.error-banner strong { font-weight: 600; }
/* Sync progress */
.progress-track { background: var(--border); border-radius: 99px; height: 8px;
                  overflow: hidden; margin: 10px 0 6px; }
.progress-fill  { background: var(--primary); height: 100%; border-radius: 99px;
                  transition: width .5s ease; min-width: 4px; }
.sync-meta      { font-size: 13px; color: var(--muted); display: flex;
                  justify-content: space-between; align-items: center; }
/* ── Dashboard sidebar layout ─────────────────────────────────── */
.app-shell { display: flex; min-height: 100vh; }
.sidebar {
  width: 220px; background: #111827; color: #fff;
  position: fixed; top: 0; left: 0; height: 100vh;
  display: flex; flex-direction: column;
  z-index: 200; overflow-y: auto; }
.sidebar-logo {
  padding: 20px 20px 16px; font-size: 15px; font-weight: 700;
  color: #fff; border-bottom: 1px solid rgba(255,255,255,.1); line-height: 1.4; }
.sidebar-logo small { display: block; font-size: 11px; font-weight: 400;
                      color: #9ca3af; margin-top: 2px; }
.sidebar-nav { padding: 12px 0; flex: 1; }
.sidebar-nav ul { list-style: none; }
.nav-item a {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 20px; font-size: 13px; font-weight: 500;
  color: #d1d5db; text-decoration: none;
  border-left: 3px solid transparent;
  transition: background .15s, color .15s, border-color .15s; }
.nav-item a:hover { background: rgba(255,255,255,.06); color: #fff; }
.nav-item.active a { background: rgba(37,99,235,.25); color: #fff;
                     border-left-color: #3b82f6; }
.nav-icon { font-size: 15px; width: 18px; text-align: center; flex-shrink: 0; }
.sidebar-footer { padding: 14px 20px; border-top: 1px solid rgba(255,255,255,.1);
                  font-size: 11px; color: #6b7280; }
/* Page content area */
.page-content { margin-left: 220px; min-height: 100vh;
                display: flex; flex-direction: column; }
.page-header { background: #111827; color: #fff; padding: 18px 32px;
               display: flex; justify-content: space-between; align-items: center;
               flex-shrink: 0; }
.page-header h1 { font-size: 18px; font-weight: 700; }
.page-header p  { font-size: 12px; color: #9ca3af; margin-top: 2px; }
.page-header-actions { display: flex; gap: 10px; align-items: center; }
.page-body { padding: 28px 32px; flex: 1;
             display: flex; flex-direction: column; gap: 20px; }
/* KPI cards */
.kpi-grid { display: grid;
            grid-template-columns: repeat(auto-fill, minmax(155px, 1fr));
            gap: 14px; }
.kpi-card { background: #fff; border-radius: 12px; padding: 18px 20px;
            box-shadow: var(--shadow-md); }
.kpi-value { font-size: 26px; font-weight: 700; color: var(--text);
             letter-spacing: -.5px; }
.kpi-value.green { color: #16a34a; }
.kpi-value.blue  { color: #2563eb; }
.kpi-value.amber { color: #d97706; }
.kpi-value.red   { color: #dc2626; }
.kpi-label { font-size: 11px; font-weight: 600; color: var(--muted);
             margin-top: 4px; text-transform: uppercase; letter-spacing: .04em; }
/* Sync warning banner */
.sync-warning { background: #fffbeb; border: 1px solid #fde68a;
                border-radius: 8px; color: #92400e;
                padding: 12px 16px; font-size: 13px; margin-bottom: 12px; }
/* Sales page */
.sales-kpi-grid { display: grid;
                  grid-template-columns: repeat(auto-fill, minmax(175px, 1fr));
                  gap: 14px; }
.chart-wrap { display: flex; align-items: center; justify-content: center;
              gap: 32px; flex-wrap: wrap; padding: 16px; }
.chart-legend { display: flex; flex-direction: column; gap: 10px; }
.legend-item { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.legend-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
/* AI analysis */
.suggestion-card { background: #fff; border-radius: 12px;
                   box-shadow: var(--shadow-md); overflow: clip; }
.suggestion-header { display: flex; align-items: center; gap: 10px;
                     padding: 14px 20px; border-bottom: 1px solid var(--border); }
.suggestion-icon { font-size: 18px; }
.suggestion-title { font-size: 14px; font-weight: 600; }
.suggestion-badge { margin-left: auto; font-size: 12px; font-weight: 600;
                    background: #eff6ff; color: #2563eb;
                    border-radius: 20px; padding: 2px 10px; }
.suggestion-tip { font-size: 13px; color: var(--muted);
                  padding: 10px 20px 4px; line-height: 1.5; }
/* Upgrade / pricing page */
.plan-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
             gap: 20px; margin-top: 4px; }
.plan-card { background: #fff; border: 2px solid var(--border); border-radius: 16px;
             padding: 28px 24px; display: flex; flex-direction: column;
             align-items: center; text-align: center; transition: border-color .2s; }
.plan-card:hover { border-color: var(--primary); }
.plan-card.featured { border-color: var(--primary);
                      box-shadow: 0 4px 20px rgba(37,99,235,.15); }
.plan-name  { font-size: 16px; font-weight: 700; margin-bottom: 6px; }
.plan-price { font-size: 28px; font-weight: 800; color: var(--primary);
              letter-spacing: -.5px; margin-bottom: 4px; }
.plan-price small { font-size: 14px; font-weight: 500; color: var(--muted); }
.plan-features { font-size: 13px; color: var(--muted); line-height: 1.7;
                 margin-bottom: 20px; flex: 1; }
.plan-cta { width: 100%; padding: 11px; font-size: 14px; font-weight: 600;
            border-radius: 10px; cursor: pointer; border: none;
            transition: background .15s; }
.plan-cta-primary { background: var(--primary); color: #fff; }
.plan-cta-primary:hover { background: var(--primary-h); }
.plan-cta-outline { background: #fff; color: var(--text);
                    border: 1.5px solid var(--border); }
.plan-cta-outline:hover { border-color: var(--primary); color: var(--primary); }
.upgrade-banner { background: #fef3c7; border: 1px solid #fde68a;
                  border-radius: 12px; padding: 16px 20px; margin-bottom: 4px;
                  font-size: 14px; color: #92400e; line-height: 1.6; }
.upgrade-banner strong { font-weight: 700; }
/* License key input */
.license-wrap { max-width: 480px; }
/* Upgrade modal */
.upm-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: flex;
               align-items: center; justify-content: center; z-index: 2000; }
.upm-box { background: #fff; border-radius: 16px; padding: 32px 36px;
           min-width: 340px; max-width: 500px; width: calc(100% - 48px);
           box-shadow: 0 20px 40px rgba(0,0,0,.2); }
.upm-box h2 { font-size: 20px; font-weight: 700; margin-bottom: 8px; }
.upm-sub { font-size: 14px; color: var(--muted); margin-bottom: 24px; line-height: 1.5; }
.upm-feature-table { width: 100%; border-collapse: collapse; margin-bottom: 24px;
                     font-size: 13px; }
.upm-feature-table th { text-align: left; padding: 6px 8px; background: #f9fafb;
                        font-weight: 600; color: var(--muted); font-size: 11px;
                        text-transform: uppercase; letter-spacing: .04em; }
.upm-feature-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.upm-feature-table td:not(:first-child) { text-align: center; }
.upm-check { color: #16a34a; font-weight: 700; }
.upm-cross { color: #9ca3af; }
.upm-actions { display: flex; gap: 10px; }
/* Sort lock */
.sort-lock { font-size: 11px; color: #d1d5db; margin-left: 4px; }
thead th[data-locked] { cursor: pointer; user-select: none; white-space: nowrap; }
thead th[data-locked]:hover { background: #fffbeb; }
/* Trial expiry banner */
.trial-banner { background: #fef3c7; border: 1px solid #fde68a; border-radius: 8px;
                color: #92400e; padding: 10px 16px; font-size: 13px;
                display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.trial-banner a { color: #92400e; font-weight: 700; white-space: nowrap;
                  text-decoration: underline; }
/* Sync remaining info */
.sync-info { font-size: 12px; color: var(--muted); margin-top: 8px; }
.sync-info.warn { color: #d97706; font-weight: 600; }
.license-note { font-size: 13px; color: var(--muted); line-height: 1.7;
                margin-bottom: 20px; }
/* Settings */
.settings-row { display: flex; align-items: flex-start;
                justify-content: space-between; gap: 16px;
                padding: 14px 0; border-bottom: 1px solid var(--border); }
.settings-row:last-child { border-bottom: none; }
.settings-label { font-size: 12px; font-weight: 600; color: var(--muted);
                  text-transform: uppercase; letter-spacing: .04em;
                  white-space: nowrap; padding-top: 2px; }
.settings-value { font-size: 14px; color: var(--text); text-align: right; }
.settings-path  { font-size: 12px; color: var(--muted); font-family: monospace;
                  word-break: break-all; text-align: right; max-width: 480px; }
"""

# JS that keeps the sticky table header just below the sticky search card.
# Runs once on load and on every resize so narrow-screen wrapping is handled.
_STICKY_JS = """
(function() {
  var SEARCH_TOP = 8;
  function updateStickyOffset() {
    var sc = document.getElementById('search-card');
    var ths = document.querySelectorAll('thead th');
    if (!sc || !ths.length) return;
    var offset = sc.offsetHeight + SEARCH_TOP;
    for (var i = 0; i < ths.length; i++) { ths[i].style.top = offset + 'px'; }
  }
  updateStickyOffset();
  window.addEventListener('resize', updateStickyOffset);
  if (window.ResizeObserver) {
    var sc = document.getElementById('search-card');
    if (sc) { new ResizeObserver(updateStickyOffset).observe(sc); }
  }
})();
"""

# Inline JS for the shutdown flow — kept in a variable to avoid f-string escaping
_SHUTDOWN_JS = """
function doShutdown() {
  if (!confirm('アプリを終了しますか？')) return;
  fetch('/shutdown', {method: 'POST'})
    .then(function() {
      document.open();
      document.write('<!DOCTYPE html><html><body style="font-family:sans-serif;padding:60px;text-align:center"><h2>アプリを終了しました</h2><p>このタブを閉じてください。</p></body></html>');
      document.close();
      setTimeout(function() { try { window.close(); } catch(e) {} }, 400);
    })
    .catch(function() {
      document.body.innerHTML = '<div style="padding:60px;text-align:center;font-family:sans-serif"><h2>アプリを終了しました</h2><p>このタブを閉じてください。</p></div>';
    });
}
"""

# Client-side column sort — sortable on all columns except # and リンク.
# Price column uses data-sort with the raw numeric value for correct ordering.
_SORT_JS = """
(function() {
  var _sortCol = -1;
  var _sortAsc  = true;

  function sortTable(colIdx) {
    var tbody = document.querySelector('table tbody');
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr')).filter(function(r) {
      return r.querySelectorAll('td').length > 1;
    });
    if (!rows.length) return;

    _sortAsc = (_sortCol === colIdx) ? !_sortAsc : true;
    _sortCol = colIdx;

    // Update sort icons
    document.querySelectorAll('thead th[data-sortable]').forEach(function(th) {
      var icon = th.querySelector('.sort-icon');
      if (!icon) return;
      var idx = parseInt(th.getAttribute('data-col'), 10);
      if (idx === colIdx) {
        icon.textContent = _sortAsc ? ' ▲' : ' ▼';
        icon.style.color = 'var(--primary)';
      } else {
        icon.textContent = ' ⇅';
        icon.style.color = '#d1d5db';
      }
    });

    rows.sort(function(a, b) {
      var aCell = a.querySelectorAll('td')[colIdx];
      var bCell = b.querySelectorAll('td')[colIdx];
      if (!aCell || !bCell) return 0;
      var aVal = aCell.getAttribute('data-sort') || aCell.textContent.trim();
      var bVal = bCell.getAttribute('data-sort') || bCell.textContent.trim();
      var aNum = parseFloat(aVal.replace(/[¥,]/g, ''));
      var bNum = parseFloat(bVal.replace(/[¥,]/g, ''));
      var cmp = (!isNaN(aNum) && !isNaN(bNum))
        ? (aNum - bNum)
        : aVal.localeCompare(bVal, 'ja');
      return _sortAsc ? cmp : -cmp;
    });
    rows.forEach(function(r) { tbody.appendChild(r); });
  }

  document.querySelectorAll('thead th[data-sortable]').forEach(function(th) {
    var icon = document.createElement('span');
    icon.className = 'sort-icon';
    icon.textContent = ' ⇅';
    th.appendChild(icon);
    th.addEventListener('click', function() {
      sortTable(parseInt(th.getAttribute('data-col'), 10));
    });
  });
})();
"""

# Upgrade modal — injected by _page_shell() on every page.
_UPGRADE_MODAL_HTML = """
<div class="upm-overlay" id="upgrade-modal" style="display:none"
     onclick="if(event.target===this)closeUpgradeModal()">
  <div class="upm-box">
    <h2>&#128274; Pro プランにアップグレード</h2>
    <p class="upm-sub" id="upm-reason"></p>
    <table class="upm-feature-table">
      <thead><tr><th>機能</th><th>無料</th><th>Pro</th></tr></thead>
      <tbody>
        <tr><td>同期回数/日</td>
            <td class="upm-cross">3回</td>
            <td class="upm-check">無制限</td></tr>
        <tr><td>保存件数</td>
            <td class="upm-cross">100件</td>
            <td class="upm-check">無制限</td></tr>
        <tr><td>並び替え</td>
            <td class="upm-cross">&#10005;</td>
            <td class="upm-check">&#10003;</td></tr>
        <tr><td>売上分析</td>
            <td class="upm-cross">&#10005;</td>
            <td class="upm-check">&#10003;</td></tr>
        <tr><td>AI 分析</td>
            <td class="upm-cross">&#10005;</td>
            <td class="upm-check">&#10003;</td></tr>
      </tbody>
    </table>
    <div class="upm-actions">
      <a href="/upgrade" class="btn btn-primary"
         style="flex:1;justify-content:center;text-decoration:none">
        Pro にアップグレード
      </a>
      <button class="btn btn-outline" onclick="closeUpgradeModal()">後で</button>
    </div>
  </div>
</div>"""

_UPGRADE_MODAL_JS = """
var _UPM_REASONS = {
  'sync_limit': '1日3回の同期上限に達しました。Proプランは無制限に同期できます。',
  'item_limit': '無料プランは100件まで保存できます。Proプランは無制限です。',
  'sort':       '並び替えはProプランの機能です。',
  'expired':    'トライアルが終了しました。引き続きご利用はProプランへアップグレードしてください。'
};
function showUpgradeModal(reason) {
  var msg = _UPM_REASONS[reason] || reason;
  var el = document.getElementById('upm-reason');
  if (el) el.textContent = msg;
  var overlay = document.getElementById('upgrade-modal');
  if (overlay) overlay.style.display = 'flex';
}
function closeUpgradeModal() {
  var overlay = document.getElementById('upgrade-modal');
  if (overlay) overlay.style.display = 'none';
}
"""

# Open-link handler: sends a fetch to /open so the backend launches Chrome
# with the saved Mercari session; also handles sync-form loading state.
_OPEN_LINK_JS = """
document.addEventListener('click', function(e) {
  var link = e.target.closest('.open-link');
  if (!link) return;
  e.preventDefault();
  fetch('/open?url=' + encodeURIComponent(link.getAttribute('data-url')))
    .catch(function() {});
});
(function() {
  var form = document.querySelector('form[action="/sync"]');
  if (!form) return;
  form.addEventListener('submit', function() {
    var btn = form.querySelector('button[type="submit"]');
    if (btn) { btn.disabled = true; btn.textContent = '同期中...'; }
  });
})();
"""

# Polling JS injected on /login while state is "checking", "logging_in", or "clearing".
# Redirects to "/" on "valid".
# Reloads /login on terminal states: "found_session", "invalid", "unknown"
#   (page re-renders with the correct UI for the new state).
_LOGIN_POLL_JS = """
(function() {
  function poll() {
    fetch('/login/status')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.state === 'valid') {
          window.location = '/';
          return;
        }
        if (d.state === 'found_session' || d.state === 'invalid' || d.state === 'unknown') {
          window.location = '/login';
          return;
        }
        // still in-progress: 'checking', 'logging_in', 'clearing'
        setTimeout(poll, 2000);
      })
      .catch(function() { setTimeout(poll, 3000); });
  }
  poll();
})();
"""

# Polling JS injected only on /?syncing=1 — polls /sync_status every 2 s,
# updates the progress bar, and redirects when sync completes or errors.
_SYNC_POLL_JS = """
(function() {
  function poll() {
    fetch('/sync_status')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        var pct = 4;
        if (d.total_steps > 0) {
          pct = d.done ? 100 : Math.max(4, Math.round((d.step_num - 1) / d.total_steps * 100));
        }
        var fill = document.getElementById('progress-fill');
        if (fill) fill.style.width = pct + '%';
        var pctEl = document.getElementById('sync-pct');
        if (pctEl) pctEl.textContent = pct + '%';
        var stepEl = document.getElementById('sync-step');
        if (stepEl) stepEl.textContent = d.step || '準備中...';
        var fracEl = document.getElementById('sync-fraction');
        if (fracEl && d.total_steps > 0)
          fracEl.textContent = d.step_num + ' / ' + d.total_steps;
        if (d.done) {
          if (d.stopped) {
            window.location = '/';
          } else if (d.error) {
            window.location = '/?error=' + encodeURIComponent(d.error);
          } else {
            window.location = '/?summary=1';
          }
          return;
        }
        setTimeout(poll, 2000);
      })
      .catch(function() { setTimeout(poll, 3000); });
  }
  poll();
})();
"""


_SALES_PIE_JS = """
(function() {
  var data = (typeof _PIE_DATA !== 'undefined') ? _PIE_DATA : [];
  var canvas = document.getElementById('pie-chart');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var cx = 100, cy = 100, r = 90;
  if (!data || !data.length) {
    ctx.fillStyle = '#e5e7eb';
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = '#6b7280'; ctx.font = '13px sans-serif';
    ctx.textAlign = 'center'; ctx.fillText('データなし', cx, cy+5);
    return;
  }
  var total = data.reduce(function(s, d) { return s + (d.value || 0); }, 0);
  if (total <= 0) return;
  var start = -Math.PI / 2;
  data.forEach(function(d) {
    var angle = (d.value / total) * Math.PI * 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, start, start + angle);
    ctx.closePath();
    ctx.fillStyle = d.color;
    ctx.fill();
    start += angle;
  });
  var legend = document.getElementById('pie-legend');
  if (legend) {
    data.forEach(function(d) {
      var pct = Math.round(d.value / total * 100);
      var item = document.createElement('div');
      item.className = 'legend-item';
      item.innerHTML = '<span class="legend-dot" style="background:' + d.color + '"></span>'
        + '<span>' + d.label + ' &nbsp;<strong>¥' + d.value.toLocaleString()
        + '</strong> (' + pct + '%)</span>';
      legend.appendChild(item);
    });
  }
})();
"""


def _badge_html(status, visibility_status=""):
    # 出品中 items with visibility_status='stopped' display as 公開停止中
    if status == "出品中" and visibility_status == "stopped":
        display = "公開停止中"
    else:
        display = status
    bg, fg = STATUS_BADGE.get(display, ("#f3f4f6", "#374151"))
    s = html_module.escape(display or "—")
    return f'<span class="badge" style="background:{bg};color:{fg}">{s}</span>'


def _price_sort_val(raw: str) -> str:
    """Strip ¥ and commas to get a numeric string for data-sort."""
    return re.sub(r"[¥,]", "", raw or "") or "0"


def _build_result_rows(products):
    rows = ""
    for i, p in enumerate(products, start=1):
        title      = html_module.escape(p[0] or "名称未取得")
        raw_price  = p[1] or "—"
        price      = html_module.escape(raw_price)
        price_sort = _price_sort_val(raw_price)
        url        = html_module.escape(p[2] or "")
        created    = html_module.escape(p[3] or "—")
        synced     = html_module.escape(p[4] or "—")
        vis_status = p[6] if len(p) > 6 else ""
        status_val = p[5] or ""
        display_status = "公開停止中" if (status_val == "出品中" and vis_status == "stopped") else status_val
        badge = _badge_html(status_val, vis_status or "")
        rows += f"""
        <tr>
          <td style="color:var(--muted);font-size:12px">{i}</td>
          <td>{title}</td>
          <td class="price" data-sort="{price_sort}">{price}</td>
          <td data-sort="{html_module.escape(display_status)}">{badge}</td>
          <td style="color:var(--muted)">{created}</td>
          <td style="color:var(--muted)">{synced}</td>
          <td><a class="link-btn open-link" data-url="{url}">開く ↗</a></td>
        </tr>"""
    return rows


# ---------------------------------------------------------------------------
# Dashboard helpers
# ---------------------------------------------------------------------------

def _parse_price_int(s) -> int:
    """Parse '¥1,000' or '1000円' → 1000. Returns 0 on failure."""
    if not s:
        return 0
    try:
        return int(re.sub(r"[¥,\s円]", "", s))
    except ValueError:
        return 0


def _get_kpi_stats() -> dict:
    """Return KPI counters for the main dashboard header / sidebar DB pill."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM mercari_products")
        total = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM mercari_products "
            "WHERE status='出品中' AND (visibility_status IS NULL OR visibility_status != 'stopped')"
        )
        active = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM mercari_products WHERE status='取引中'")
        trading = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM mercari_products WHERE status IN ('売却済み','販売履歴')"
        )
        sold = cursor.fetchone()[0]
        cursor.execute(
            "SELECT price FROM mercari_products WHERE status IN ('売却済み','販売履歴')"
        )
        total_sales = sum(
            _parse_price_int(r[0]) for r in cursor.fetchall()
        )
        cursor.execute("SELECT MAX(synced_at) FROM mercari_products")
        row = cursor.fetchone()
        last_sync = (row[0] or "")[:16] if row else ""
        conn.close()
    except Exception:
        return {"total": 0, "active": 0, "trading": 0, "sold": 0,
                "total_sales": 0, "last_sync": "–"}
    return {
        "total": total,
        "active": active,
        "trading": trading,
        "sold": sold,
        "total_sales": total_sales,
        "last_sync": last_sync or "–",
    }


_NAV_ITEMS = [
    ("main",     "/",         "🏠", "メイン"),
    ("products", "/products", "📦", "商品管理"),
    ("sales",    "/sales",    "📊", "売上分析"),
    ("ai",       "/ai",       "🤖", "AI 分析"),
    ("settings", "/settings", "⚙️",  "設定"),
]


def _sidebar(active: str) -> str:
    items = ""
    for key, href, icon, label in _NAV_ITEMS:
        cls = "nav-item active" if key == active else "nav-item"
        items += (
            f'<li class="{cls}"><a href="{href}">'
            f'<span class="nav-icon">{icon}</span>'
            f'{html_module.escape(label)}</a></li>\n'
        )
    return f"""<nav class="sidebar">
  <div class="sidebar-logo">Mercari 在庫管理<small>ダッシュボード</small></div>
  <div class="sidebar-nav"><ul>{items}</ul></div>
  <div class="sidebar-footer">{APP_VERSION}</div>
</nav>"""


def _page_shell(title: str, active: str, content: str,
                extra_js: str = "", subtitle: str = "") -> str:
    """Return a full HTML page with sidebar layout."""
    stats = _get_kpi_stats()
    sub_html = f'<p>{html_module.escape(subtitle)}</p>' if subtitle else ""

    # Trial expiry banner (shown ≤2 days remaining so all 3 trial days get a warning)
    _plan_shell = _check_plan()
    if _plan_shell == "trial":
        _days_left = _trial_days_remaining()
        if _days_left <= 2:
            trial_banner_html = (
                f'<div class="trial-banner">'
                f'<span>&#9888; トライアルはあと <strong>{_days_left} 日</strong>で終了します。'
                f'データはそのまま保持されます。</span>'
                f'<a href="/upgrade">今すぐアップグレード →</a>'
                f'</div>'
            )
        else:
            trial_banner_html = ""
    else:
        trial_banner_html = ""

    # Inject sidebar nav-lock when a sync is in progress so users cannot
    # navigate away mid-sync from any page (not just the main sync page).
    sync_lock_js = ""
    if _sync_running:
        sync_lock_js = """
(function() {
  document.querySelectorAll('.nav-item a').forEach(function(a) {
    a.addEventListener('click', function(e) {
      e.preventDefault();
      alert('同期中です。完了するまでお待ちください。');
    });
  });
})();
"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html_module.escape(title)} — Mercari 在庫管理</title>
  <style>{_CSS}</style>
</head>
<body>
<div class="app-shell">
  {_sidebar(active)}
  <div class="page-content">
    <div class="page-header">
      <div>
        <h1>{html_module.escape(title)}</h1>
        {sub_html}
      </div>
      <div class="page-header-actions">
        <span class="db-pill">DB: {stats['total']} 件</span>
        <button class="btn-exit" onclick="doShutdown()">終了</button>
      </div>
    </div>
    <div class="page-body">
      {trial_banner_html}
      {content}
    </div>
  </div>
</div>
{_UPGRADE_MODAL_HTML}
<script>
{_SHUTDOWN_JS}
{_UPGRADE_MODAL_JS}
{sync_lock_js}
{extra_js}
</script>
</body>
</html>"""


@app.route("/")
def home():
    global _session_state, _session_last_login
    if _session_state == "unknown":
        # File-based check: never open Chrome on startup.
        # If cookie file exists, show "found session" screen so user can choose.
        # Only start Chrome when the user explicitly clicks "ログインを開始".
        if COOKIE_FILE and os.path.exists(COOKIE_FILE):
            _session_state = "found_session"
            try:
                mtime = os.path.getmtime(COOKIE_FILE)
                _session_last_login = datetime.fromtimestamp(mtime, tz=_JST).strftime(
                    "%Y-%m-%d %H:%M"
                )
            except Exception:
                pass
        else:
            _session_state = "invalid"
        return redirect("/login")
    if _session_state != "valid":
        return redirect("/login")

    # Plan gate: redirect to upgrade screen when trial has expired
    plan = _check_plan()
    if plan == "expired":
        return redirect("/upgrade")

    show_summary       = request.args.get("summary") == "1" and bool(_last_sync_summary)
    error_param        = request.args.get("error", "")
    licensed           = request.args.get("licensed") == "1"
    syncing            = request.args.get("syncing") == "1" or _sync_running
    upgrade_modal_param = request.args.get("upgrade_modal", "")

    stats = _get_kpi_stats()

    # ── KPI cards ─────────────────────────────────────────────────────────
    kpi_html = f"""
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-value">{stats['total']}</div>
        <div class="kpi-label">総商品数</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value green">{stats['active']}</div>
        <div class="kpi-label">出品中</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value amber">{stats['trading']}</div>
        <div class="kpi-label">取引中</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value blue">{stats['sold']}</div>
        <div class="kpi-label">売却済み</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value red" style="font-size:20px">¥{stats['total_sales']:,}</div>
        <div class="kpi-label">推定売上合計</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value" style="font-size:14px;letter-spacing:0">
          {html_module.escape(stats['last_sync'])}
        </div>
        <div class="kpi-label">最終同期</div>
      </div>
    </div>"""

    # ── Sync checkboxes ────────────────────────────────────────────────────
    sync_cbs = ""
    for s in STATUSES:
        sync_cbs += (f'<label class="cb-label">'
                     f'<input type="checkbox" name="statuses" value="{s}" checked> {s}'
                     f'</label>\n')

    # ── Sync card ──────────────────────────────────────────────────────────
    if syncing:
        sync_card = f"""
        <div class="card" id="sync-card">
          <div class="card-header">
            <span class="card-title">同期中</span>
            <div style="display:flex;gap:8px;align-items:center">
              <span id="sync-pct" class="db-pill"
                    style="background:rgba(37,99,235,.12);color:#2563eb">…</span>
              <button id="btn-force-stop"
                      style="background:#ef4444;color:#fff;border:none;border-radius:8px;
                             padding:6px 14px;font-size:13px;font-weight:600;cursor:pointer"
                      onclick="if(confirm('同期を強制停止しますか？\\n現在処理中のデータは保存されます。')){{
                        this.disabled=true;this.textContent='停止中…';
                        fetch('/sync/stop',{{method:'POST'}}).catch(function(){{}});
                      }}">
                &#9632; 強制停止
              </button>
            </div>
          </div>
          <div class="card-body">
            <div class="sync-warning">
              &#9888; 同期中です。ブラウザウィンドウは操作しないでください。
            </div>
            <div class="progress-track">
              <div class="progress-fill" id="progress-fill" style="width:4%"></div>
            </div>
            <div class="sync-meta">
              <span id="sync-step">準備中...</span>
              <span id="sync-fraction"></span>
            </div>
          </div>
        </div>"""
    else:
        # Sync button: disabled with upgrade CTA when free plan is at daily limit
        if plan == "free":
            _used = _get_daily_sync_count()
            _remaining = max(0, _FREE_SYNC_LIMIT - _used)
            if _remaining == 0:
                _sync_btn_html = (
                    '<button class="btn btn-primary" type="button" disabled '
                    'style="font-size:15px;padding:11px 28px">'
                    '&#x21BB; 同期を開始</button>'
                    '<p class="sync-info warn" style="margin-top:8px">'
                    f'本日の同期上限（{_FREE_SYNC_LIMIT}回）に達しました。'
                    ' <a href="/upgrade" style="color:#d97706;font-weight:700">'
                    'Pro にアップグレード →</a></p>'
                )
            else:
                _warn_cls = " warn" if _remaining == 1 else ""
                _sync_btn_html = (
                    '<button class="btn btn-primary" type="submit" '
                    'style="font-size:15px;padding:11px 28px">'
                    '&#x21BB; 同期を開始</button>'
                    f'<p class="sync-info{_warn_cls}" style="margin-top:8px">'
                    f'本日の残り同期回数: {_remaining} / {_FREE_SYNC_LIMIT}</p>'
                )
        else:
            _sync_btn_html = (
                '<button class="btn btn-primary" type="submit" '
                'style="font-size:15px;padding:11px 28px">'
                '&#x21BB; 同期を開始</button>'
            )

        sync_card = f"""
        <div class="card" id="sync-card">
          <div class="card-header">
            <span class="card-title">同期</span>
          </div>
          <div class="card-body">
            <form method="POST" action="/sync">
              <p class="field-label">同期するステータス</p>
              <div class="cb-row">{sync_cbs}</div>
              {_sync_btn_html}
            </form>
          </div>
        </div>"""

    # ── Error banner ───────────────────────────────────────────────────────
    if error_param:
        import urllib.parse
        if error_param == "sync_running":
            _err_display = "同期はすでに実行中です。完了をお待ちください。"
        else:
            _err_display = urllib.parse.unquote(error_param)
        error_banner = (
            f'<div class="error-banner">'
            f'<strong>エラー:</strong> {html_module.escape(_err_display)}'
            f'</div>'
        )
    else:
        error_banner = ""

    # ── Sync summary modal ─────────────────────────────────────────────────
    if show_summary:
        s = _last_sync_summary
        fetched_rows = "".join(
            f"<tr><td>取得: {html_module.escape(st)}</td><td>{cnt} 件</td></tr>"
            for st, cnt in s.get("per_status", {}).items()
        )
        db_rows = "".join(
            f"<tr><td>DB: {html_module.escape(st)}</td><td>{cnt} 件</td></tr>"
            for st, cnt in s.get("db_by_status", {}).items()
        )
        summary_modal = f"""
        <div class="modal-overlay" id="summary-modal">
          <div class="modal-box">
            <h2>同期完了</h2>
            <table class="modal-table">
              <tr><td>開始時刻</td><td>{html_module.escape(s.get('start_jst',''))}</td></tr>
              <tr><td>終了時刻</td><td>{html_module.escape(s.get('end_jst',''))}</td></tr>
              <tr><td>所要時間</td><td>{s.get('elapsed', 0)} 秒</td></tr>
              <tr><td>検出合計</td><td>{s.get('total', 0)} 件</td></tr>
              <tr><td>新増</td><td>{s.get('inserted', 0)} 件</td></tr>
              <tr><td>更新</td><td>{s.get('updated', 0)} 件</td></tr>
              <tr><td>スキップ</td><td>{s.get('skipped', 0)} 件</td></tr>
              {fetched_rows}
              <tr><td colspan="2"
                  style="padding-top:6px;font-weight:600;font-size:12px;color:#6b7280">
                DB合計</td></tr>
              {db_rows}
              <tr><td>DB合計</td><td>{s.get('db_total', '–')} 件</td></tr>
            </table>
            <button class="modal-close"
                    onclick="document.getElementById('summary-modal').remove()">
              閉じる
            </button>
          </div>
        </div>"""
    else:
        summary_modal = ""

    licensed_banner = (
        '<div class="error-banner" style="background:#dcfce7;border-color:#86efac;color:#166534">'
        '&#10003; ライセンスが有効化されました。すべての機能が利用可能です。'
        '</div>'
    ) if licensed else ""

    content = f"""
    {licensed_banner}
    {error_banner}
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
      <div></div>
      {_license_badge_html()}
    </div>
    {kpi_html}
    {sync_card}
    {summary_modal}"""

    _modal_show_js = (
        f'showUpgradeModal({json.dumps(upgrade_modal_param)});'
        if upgrade_modal_param else ""
    )
    extra_js = f"""
(function() {{
  var form = document.querySelector('form[action="/sync"]');
  if (form) {{
    form.addEventListener('submit', function() {{
      var btn = form.querySelector('button[type="submit"]');
      if (btn) {{ btn.disabled = true; btn.textContent = '同期中...'; }}
    }});
  }}
}})();
{_modal_show_js}
{"" if not syncing else _SYNC_POLL_JS}"""

    return _page_shell("メインダッシュボード", "main", content, extra_js,
                       subtitle="同期・KPIモニタリング")


@app.route("/sync", methods=["POST"])
def sync():
    global _sync_running, _sync_progress
    if _sync_running:
        return redirect("/?error=sync_running")

    # Plan gate: expired users cannot sync
    _plan_now = _check_plan()
    if _plan_now == "expired":
        return redirect("/upgrade")
    # Free-plan daily sync limit
    if _plan_now == "free" and _get_daily_sync_count() >= _FREE_SYNC_LIMIT:
        return redirect("/?upgrade_modal=sync_limit")

    selected = request.form.getlist("statuses") or list(STATUSES)
    _sync_running = True
    _sync_progress = {
        "running":     True,
        "done":        False,
        "step":        "準備中",
        "step_num":    0,
        "total_steps": len(selected),
        "fetched":     0,
        "error":       "",
        "stopped":     False,
    }

    def _bg_sync():
        global _sync_running, _sync_progress, _sync_stop_requested
        _sync_stop_requested = False   # clear any leftover stop flag from previous run
        try:
            run_scraper(selected_statuses=selected)
            if _check_plan() == "free":
                _increment_sync_count()
                _enforce_free_item_limit()
        except _SyncStopped:
            print("[sync] 強制停止されました")
            _sync_progress["stopped"] = True
        except Exception as exc:
            import traceback
            print("[sync] 同期エラー:")
            traceback.print_exc()
            _sync_progress["error"] = str(exc).split("\n")[0][:200]
        finally:
            _sync_running = False
            _sync_progress["running"] = False
            _sync_progress["done"]    = True

    threading.Thread(target=_bg_sync, daemon=True, name="sync-bg").start()
    return redirect("/?syncing=1")


@app.route("/sync_status")
def sync_status():
    from flask import jsonify
    return jsonify(_sync_progress)


@app.route("/sync/stop", methods=["POST"])
def sync_stop():
    """Signal the background sync to abort and quit the Selenium driver immediately."""
    global _sync_stop_requested, _singleton_driver
    _sync_stop_requested = True
    with _driver_lock:
        if _singleton_driver is not None:
            try:
                _singleton_driver.quit()
            except Exception:
                pass
            _singleton_driver = None
    return Response("ok", status=200, headers={"Content-Type": "text/plain"})


@app.route("/login")
def login_page():
    state = _session_state

    if state in ("checking", "logging_in", "clearing"):
        if state == "logging_in":
            status_label = "ログイン中... ブラウザで Mercari にログインしてください"
        elif state == "clearing":
            status_label = "セッションデータを削除中..."
        else:
            status_label = "セッションを確認中..."
        body_content = f"""
      <p class="login-status">{status_label}</p>
      <div class="spinner"></div>"""
        poll_js = _LOGIN_POLL_JS

    elif state == "found_session":
        last_login_html = (
            f'<p class="login-info">最終ログイン: {html_module.escape(_session_last_login)}</p>'
            if _session_last_login else ""
        )
        body_content = f"""
      <p class="login-badge">&#10003; ログイン済み</p>
      {last_login_html}
      <div class="login-btn-group">
        <form method="POST" action="/login/use">
          <button class="btn btn-primary login-btn" type="submit">
            既存のセッションを使用
          </button>
        </form>
        <form method="POST" action="/login/relogin">
          <button class="btn btn-outline login-btn" type="submit">
            再ログイン
          </button>
        </form>
        <form method="POST" action="/login/clear">
          <button class="btn btn-danger login-btn" type="submit"
                  onclick="return confirm('ログインデータを削除しますか？\\nChromeプロファイルとCookieが削除されます。')">
            ログインデータを削除
          </button>
        </form>
      </div>"""
        poll_js = ""

    else:
        # "invalid" or anything unexpected — show the login button
        body_content = """
      <p class="login-desc">Mercari の在庫を同期するには、<br>Mercari アカウントでログインが必要です。</p>
      <form method="POST" action="/login/start">
        <button class="btn btn-primary login-btn" type="submit">
          ログインを開始
        </button>
      </form>"""
        poll_js = ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Mercari ログイン</title>
  <style>{_CSS}
  .login-card {{ max-width: 480px; margin: 80px auto; }}
  .login-desc {{ font-size: 15px; color: var(--muted); line-height: 1.6;
                margin-bottom: 24px; text-align: center; }}
  .login-status {{ font-size: 15px; color: var(--muted); margin-bottom: 20px;
                  text-align: center; }}
  .login-badge {{ font-size: 17px; font-weight: 700; color: #166534;
                 margin-bottom: 6px; text-align: center; }}
  .login-info {{ font-size: 13px; color: var(--muted); margin-bottom: 24px;
                text-align: center; }}
  .login-btn-group {{ display: flex; flex-direction: column; gap: 10px; }}
  .login-btn {{ width: 100%; padding: 13px; font-size: 15px; justify-content: center; }}
  .btn-danger {{ background: #ef4444; color: #fff; border: none; }}
  .btn-danger:hover {{ background: #dc2626; }}
  .spinner {{ width: 36px; height: 36px; border: 4px solid var(--border);
             border-top-color: var(--primary); border-radius: 50%;
             animation: spin .8s linear infinite; margin: 0 auto; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
<header>
  <div class="header-inner">
    <div>
      <h1>Mercari 在庫管理</h1>
      <p>Mercari 販売者向け在庫管理ツール</p>
    </div>
  </div>
</header>
<main>
  <div class="card login-card">
    <div class="card-header">
      <span class="card-title">Mercari ログイン</span>
    </div>
    <div class="card-body" style="text-align:center;padding:32px">
      {body_content}
    </div>
  </div>
</main>
<script>{poll_js}</script>
</body>
</html>"""


@app.route("/login/start", methods=["POST"])
def login_start():
    global _session_state
    if _session_state == "logging_in":
        return redirect("/login")
    _session_state = "logging_in"
    threading.Thread(target=_do_login, daemon=True, name="login-thread").start()
    return redirect("/login")


@app.route("/login/use", methods=["POST"])
def login_use():
    """Accept the existing session and proceed to the main screen."""
    global _session_state
    if _session_state == "found_session":
        _session_state = "valid"
    return redirect("/")


@app.route("/login/relogin", methods=["POST"])
def login_relogin():
    """Discard the existing session and force a fresh interactive login."""
    global _session_state
    if _session_state == "logging_in":
        return redirect("/login")
    _session_state = "logging_in"
    threading.Thread(
        target=lambda: _do_login(force_relogin=True),
        daemon=True,
        name="login-relogin",
    ).start()
    return redirect("/login")


@app.route("/login/clear", methods=["POST"])
def login_clear():
    """Delete all stored session data (cookies + Chrome profile) and log out."""
    global _session_state
    if _session_state == "clearing":
        return redirect("/login")
    _session_state = "clearing"
    threading.Thread(target=_clear_session, daemon=True, name="session-clear").start()
    return redirect("/login")


@app.route("/login/status")
def login_status():
    from flask import jsonify
    return jsonify({"state": _session_state, "last_login": _session_last_login})


@app.route("/open")
def open_url():
    """Open a Mercari product URL in a new tab of the singleton Chrome driver.

    Because the singleton uses a persistent Chrome profile with a live Mercari
    session, the product page opens already logged in — no cookie injection
    needed. If sync is currently running (_sync_running) or the driver is
    unavailable, falls back to the system browser.
    """
    url = request.args.get("url", "")
    if not url.startswith("https://jp.mercari.com/"):
        return Response("Invalid URL", status=400)

    def _open_tab():
        if _sync_running:
            # Singleton is busy navigating listing pages; use system browser
            print(f"[open] 同期中のためフォールバック: {url}")
            webbrowser.open(url)
            return
        try:
            driver = _get_or_create_driver()
            try:
                driver.maximize_window()
            except Exception:
                pass
            # Open the product URL in a new tab within the existing Chrome window
            driver.execute_script("window.open(arguments[0], '_blank');", url)
        except Exception as exc:
            print(f"[open] 新タブ失敗 — fallback: {exc}")
            webbrowser.open(url)

    threading.Thread(target=_open_tab, daemon=True).start()
    # Close the intermediate /open tab that the browser opened
    return Response(
        '<!DOCTYPE html><html><head>'
        '<script>window.close();</script>'
        '</head><body></body></html>',
        mimetype="text/html",
    )


@app.route("/shutdown", methods=["POST"])
def shutdown():
    # Give the browser enough time to receive this response before the process exits
    threading.Thread(
        target=lambda: (time.sleep(0.8), os._exit(0)),
        daemon=True,
    ).start()
    return Response("ok", status=200, headers={"Content-Type": "text/plain"})


@app.route("/export/csv")
def export_csv():
    q            = request.args.get("q", "").strip()
    sel_statuses = request.args.getlist("statuses") or list(FILTER_STATUSES)
    products     = _query_products(q, sel_statuses)

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM
    writer = csv.writer(buf)
    writer.writerow(["状態", "商品名", "価格", "商品登録時間", "抓取時間", "リンク"])
    for p in products:
        vis = p[6] if len(p) > 6 else ""
        display_status = "公開停止中" if (p[5] == "出品中" and vis == "stopped") else (p[5] or "")
        writer.writerow([display_status, p[0] or "", p[1] or "",
                         p[3] or "", p[4] or "", p[2] or ""])

    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=mercari_export.csv"},
    )


@app.route("/export/xlsx")
def export_xlsx():
    q            = request.args.get("q", "").strip()
    sel_statuses = request.args.getlist("statuses") or list(FILTER_STATUSES)
    products     = _query_products(q, sel_statuses)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Mercari商品"
    ws.append(["状態", "商品名", "価格", "商品登録時間", "抓取時間", "リンク"])
    for p in products:
        vis = p[6] if len(p) > 6 else ""
        display_status = "公開停止中" if (p[5] == "出品中" and vis == "stopped") else (p[5] or "")
        ws.append([display_status, p[0] or "", p[1] or "",
                   p[3] or "", p[4] or "", p[2] or ""])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=mercari_export.xlsx"},
    )


# ---------------------------------------------------------------------------
# Upgrade / pricing page
# ---------------------------------------------------------------------------

@app.route("/upgrade")
def upgrade_page():
    if _session_state != "valid":
        return redirect("/login")

    plan    = _check_plan()
    from_pg = request.args.get("from", "")

    if from_pg == "sales":
        banner_msg = "売上分析は月額プランまたは買い切りプランでご利用いただけます。"
    elif from_pg == "ai":
        banner_msg = "AI分析は月額プランまたは買い切りプランでご利用いただけます。"
    elif plan in ("expired", "free"):
        banner_msg = "3日間のトライアル期間が終了しました。引き続きご利用にはプランを選択してください。"
    else:
        banner_msg = ""

    banner_html = (
        f'<div class="upgrade-banner"><strong>&#9888; </strong>{html_module.escape(banner_msg)}</div>'
        if banner_msg else ""
    )

    free_features = "同期: 1日3回<br>商品管理: 100件まで<br>並び替え: ✗<br>売上分析: ✗<br>AI分析: ✗"
    monthly_features = "同期: 無制限<br>商品管理: 無制限<br>並び替え: ○<br>売上分析: ○<br>AI分析: ○"
    lifetime_features = "同期: 無制限<br>商品管理: 無制限<br>並び替え: ○<br>売上分析: ○<br>AI分析: ○<br>将来のアップデート: ○"

    plan_grid = f"""
    <div class="plan-grid">
      <div class="plan-card">
        <div class="plan-name">無料版</div>
        <div class="plan-price">¥0</div>
        <div class="plan-features">{free_features}</div>
        <form method="POST" action="/license/choose-free" style="width:100%">
          <button class="plan-cta plan-cta-outline" type="submit">無料版で続ける</button>
        </form>
      </div>
      <div class="plan-card featured">
        <div class="plan-name">月額プラン</div>
        <div class="plan-price">¥480<small>/月</small></div>
        <div class="plan-features">{monthly_features}</div>
        <a class="plan-cta plan-cta-primary" href="/settings"
           style="display:block;text-decoration:none">
          月額プランに登録
        </a>
      </div>
      <div class="plan-card">
        <div class="plan-name">買い切り</div>
        <div class="plan-price">¥1,980</div>
        <div class="plan-features">{lifetime_features}</div>
        <a class="plan-cta plan-cta-primary" href="/settings"
           style="display:block;text-decoration:none">
          今すぐ購入
        </a>
      </div>
    </div>"""

    content = f"{banner_html}\n{plan_grid}"
    return _page_shell("プランを選択", "main", content,
                       subtitle="Mercari 販売者向け在庫管理ツール — プランをアップグレードしてすべての機能を利用できます")


@app.route("/license/choose-free", methods=["POST"])
def license_choose_free():
    """User opts into the free plan (persists so upgrade screen is not shown again)."""
    state = _get_license()
    state["plan"] = "free"
    _save_license(state)
    return redirect("/")


@app.route("/license", methods=["GET", "POST"])
def license_page():
    """Kept for backward compatibility — activation moved to /settings."""
    return redirect("/settings")


# ---------------------------------------------------------------------------
# Product Management page
# ---------------------------------------------------------------------------

@app.route("/products")
def products_page():
    if _session_state != "valid":
        return redirect("/login")

    searched     = request.args.get("searched") == "1"
    q            = request.args.get("q", "").strip()
    sel_statuses = request.args.getlist("statuses") or list(FILTER_STATUSES)
    price_min    = request.args.get("price_min", "").strip()
    price_max    = request.args.get("price_max", "").strip()

    products = _query_products(q, sel_statuses) if searched else []

    if searched and (price_min or price_max):
        filtered = []
        for p in products:
            v = _parse_price_int(p[1])
            if price_min:
                try:
                    if v < int(price_min):
                        continue
                except ValueError:
                    pass
            if price_max:
                try:
                    if v > int(price_max):
                        continue
                except ValueError:
                    pass
            filtered.append(p)
        products = filtered

    count = len(products)
    sort_locked = _check_plan() in ("free", "expired")

    if searched:
        per_status = {}
        for p in products:
            s = p[5] or "出品中"
            vis = p[6] if len(p) > 6 else ""
            label = "公開停止中" if (s == "出品中" and vis == "stopped") else s
            per_status[label] = per_status.get(label, 0) + 1
        status_summary = ", ".join(f"{s}={c}" for s, c in per_status.items())
        print(f"[products] selected: {', '.join(sel_statuses)}")
        print(f"[products] result: {status_summary or '0件'} (total={count})")

    search_cbs = ""
    for s in FILTER_STATUSES:
        chk = "checked" if s in sel_statuses else ""
        search_cbs += (f'<label class="cb-label">'
                       f'<input type="checkbox" name="statuses" value="{s}" {chk}> {s}'
                       f'</label>\n')

    search_card = f"""
    <div class="card" id="search-card">
      <div class="card-header">
        <span class="card-title">商品を検索</span>
      </div>
      <div class="card-body">
        <form method="GET" action="/products">
          <input type="hidden" name="searched" value="1">
          <div class="search-row">
            <input class="text-input" type="text" name="q"
                   placeholder="商品名で検索…" value="{html_module.escape(q)}">
            <button class="btn btn-primary" type="submit">検索</button>
          </div>
          <p class="field-label">ステータスで絞り込み</p>
          <div class="cb-row">{search_cbs}</div>
          <div style="display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap">
            <span class="field-label" style="margin:0;white-space:nowrap">価格範囲</span>
            <input class="text-input" type="number" name="price_min"
                   placeholder="最低価格 (円)" value="{html_module.escape(price_min)}"
                   style="flex:0 0 150px">
            <span style="color:var(--muted)">〜</span>
            <input class="text-input" type="number" name="price_max"
                   placeholder="最高価格 (円)" value="{html_module.escape(price_max)}"
                   style="flex:0 0 150px">
          </div>
        </form>
      </div>
    </div>"""

    if not searched:
        results_html = ""
        export_js = ""
    else:
        rows_html = _build_result_rows(products)
        disabled  = "disabled" if count == 0 else ""
        empty_row = (
            '<tr><td colspan="7"><div class="empty-state">'
            '<div class="es-icon">🔍</div>'
            '<p>該当する商品が見つかりませんでした</p>'
            '</div></td></tr>'
        ) if count == 0 else ""
        results_html = f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">検索結果
              <span class="count-badge">{count} 件</span>
            </span>
            <div class="export-row">
              <a class="btn btn-outline" id="export-csv" href="#" {disabled}>⬇ CSV</a>
              <a class="btn btn-outline" id="export-xlsx" href="#" {disabled}>⬇ Excel</a>
            </div>
          </div>
          <div class="card-body" style="padding:0">
            <table>
              <thead>
                <tr>
                  <th style="width:40px">#</th>
                  {''.join([
                    f'<th data-locked data-col="{c}" onclick="showUpgradeModal(\'sort\')">{lbl} <span class="sort-lock">&#128274;</span></th>'
                    if sort_locked else
                    f'<th data-sortable data-col="{c}">{lbl}</th>'
                    for c, lbl in [(1,"商品名"),(2,"価格"),(3,"状態"),(4,"商品登録時間"),(5,"抓取時間")]
                  ])}
                  <th>リンク</th>
                </tr>
              </thead>
              <tbody>{rows_html}{empty_row}</tbody>
            </table>
          </div>
        </div>"""
        export_js = """
const sp = new URLSearchParams(window.location.search);
const csv_el  = document.getElementById('export-csv');
const xlsx_el = document.getElementById('export-xlsx');
if (csv_el  && !csv_el.hasAttribute('disabled'))
    csv_el.href  = '/export/csv?'  + sp.toString();
if (xlsx_el && !xlsx_el.hasAttribute('disabled'))
    xlsx_el.href = '/export/xlsx?' + sp.toString();"""

    content  = search_card + "\n" + results_html
    _sort_js_include = "" if sort_locked else _SORT_JS
    extra_js = f"{_STICKY_JS}\n{_sort_js_include}\n{_OPEN_LINK_JS}\n{export_js}"
    return _page_shell("商品管理", "products", content, extra_js,
                       subtitle="商品の検索・エクスポート")


# ---------------------------------------------------------------------------
# Sales Performance page
# ---------------------------------------------------------------------------

@app.route("/sales")
def sales_page():
    if _session_state != "valid":
        return redirect("/login")
    if _check_plan() in ("expired", "free"):
        return redirect("/upgrade?from=sales")

    range_param = request.args.get("range", "all")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    if range_param == "30d":
        cutoff = (datetime.now(tz=_JST) - timedelta(days=30)).strftime("%Y-%m-%d")
        cursor.execute(
            "SELECT title, price, status, created_at, synced_at "
            "FROM mercari_products "
            "WHERE status IN ('売却済み','販売履歴') AND synced_at >= ?",
            (cutoff,),
        )
    else:
        cursor.execute(
            "SELECT title, price, status, created_at, synced_at "
            "FROM mercari_products WHERE status IN ('売却済み','販売履歴')"
        )
    rows = cursor.fetchall()
    conn.close()

    valid_prices = [_parse_price_int(r[1]) for r in rows if _parse_price_int(r[1]) > 0]
    total_sales  = sum(valid_prices)
    sold_count   = len(rows)
    avg_price    = (sum(valid_prices) // len(valid_prices)) if valid_prices else 0
    est_fee      = int(total_sales * 0.10)
    est_profit   = total_sales - est_fee

    kpi_html = f"""
    <div class="sales-kpi-grid">
      <div class="kpi-card">
        <div class="kpi-value red" style="font-size:20px">¥{total_sales:,}</div>
        <div class="kpi-label">売上合計（推定）</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value blue">{sold_count}</div>
        <div class="kpi-label">売却件数</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value" style="font-size:20px">¥{avg_price:,}</div>
        <div class="kpi-label">平均単価</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value amber" style="font-size:20px">¥{est_fee:,}</div>
        <div class="kpi-label">推定手数料（10%）</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value green" style="font-size:20px">¥{est_profit:,}</div>
        <div class="kpi-label">推定利益（手数料後）</div>
      </div>
    </div>"""

    pie_data = json.dumps([
        {"label": "推定利益 (90%)", "value": est_profit, "color": "#22c55e"},
        {"label": "手数料 (10%)",   "value": est_fee,    "color": "#f59e0b"},
    ]) if total_sales > 0 else "[]"

    range_all = "btn btn-primary" if range_param != "30d" else "btn btn-outline"
    range_30d = "btn btn-primary" if range_param == "30d" else "btn btn-outline"
    filter_html = f"""
    <div style="display:flex;gap:8px">
      <a class="{range_all}" href="/sales?range=all">全期間</a>
      <a class="{range_30d}" href="/sales?range=30d">過去30日</a>
    </div>"""

    chart_card = f"""
    <div class="card">
      <div class="card-header"><span class="card-title">売上内訳（推定）</span></div>
      <div class="card-body">
        <div class="chart-wrap">
          <canvas id="pie-chart" width="200" height="200"></canvas>
          <div class="chart-legend" id="pie-legend"></div>
        </div>
        <p style="font-size:12px;color:var(--muted);text-align:center;margin-top:4px">
          ※ 手数料はMercari標準（10%）で推定。送料データは現在非対応です。
        </p>
      </div>
    </div>"""

    table_rows = ""
    for r in rows:
        title, price, status, created_at, synced_at = r
        badge = _badge_html(status)
        table_rows += (
            f"<tr>"
            f"<td>{html_module.escape(title or '')}</td>"
            f"<td class='price'>{html_module.escape(price or '')}</td>"
            f"<td>{badge}</td>"
            f"<td style='color:var(--muted)'>{html_module.escape(created_at or '')}</td>"
            f"<td style='font-size:12px;color:var(--muted)'>"
            f"{html_module.escape((synced_at or '')[:16])}</td>"
            f"</tr>"
        )
    if not table_rows:
        table_rows = (
            '<tr><td colspan="5"><div class="empty-state">'
            '<div class="es-icon">📊</div>'
            '<p>売却済み商品がありません</p>'
            '</div></td></tr>'
        )

    table_card = f"""
    <div class="card">
      <div class="card-header">
        <span class="card-title">売却商品一覧
          <span class="count-badge">{sold_count} 件</span>
        </span>
      </div>
      <div class="card-body" style="padding:0">
        <table>
          <thead>
            <tr>
              <th data-sortable data-col="0">商品名</th>
              <th data-sortable data-col="1">価格</th>
              <th data-sortable data-col="2">状態</th>
              <th data-sortable data-col="3">商品登録日</th>
              <th data-sortable data-col="4">最終確認</th>
            </tr>
          </thead>
          <tbody>{table_rows}</tbody>
        </table>
      </div>
    </div>"""

    content  = f"{filter_html}\n{kpi_html}\n{chart_card}\n{table_card}"
    extra_js = f"var _PIE_DATA = {pie_data};\n{_SALES_PIE_JS}\n{_SORT_JS}"
    return _page_shell("売上分析", "sales", content, extra_js,
                       subtitle="売却済み商品の実績")


# ---------------------------------------------------------------------------
# AI Analysis page
# ---------------------------------------------------------------------------

@app.route("/ai")
def ai_page():
    if _session_state != "valid":
        return redirect("/login")
    if _check_plan() in ("expired", "free"):
        return redirect("/upgrade?from=ai")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT title, price, item_url FROM mercari_products "
        "WHERE status='出品中' AND visibility_status='stopped' ORDER BY synced_at DESC"
    )
    stopped_items = cursor.fetchall()

    sixty_ago = (datetime.now(tz=_JST) - timedelta(days=60)).strftime("%Y-%m-%d")
    cursor.execute(
        "SELECT title, price, item_url, created_at FROM mercari_products "
        "WHERE status='出品中' AND (visibility_status IS NULL OR visibility_status!='stopped') "
        "AND created_at <= ? AND created_at != '' ORDER BY created_at ASC LIMIT 50",
        (sixty_ago,),
    )
    long_items = cursor.fetchall()

    cursor.execute(
        "SELECT price FROM mercari_products "
        "WHERE status='出品中' AND (visibility_status IS NULL OR visibility_status!='stopped')"
    )
    active_prices = [_parse_price_int(r[0]) for r in cursor.fetchall() if _parse_price_int(r[0]) > 0]
    avg_active = (sum(active_prices) // len(active_prices)) if active_prices else 0

    if avg_active > 0:
        cursor.execute(
            "SELECT title, price, item_url FROM mercari_products "
            "WHERE status='出品中' AND (visibility_status IS NULL OR visibility_status!='stopped') "
            "ORDER BY id DESC"
        )
        all_active = cursor.fetchall()
        high_items = [(t, p, u) for t, p, u in all_active
                      if _parse_price_int(p) >= avg_active * 2][:20]
        low_items  = [(t, p, u) for t, p, u in all_active
                      if 0 < _parse_price_int(p) <= avg_active // 2][:20]
    else:
        high_items = low_items = []

    cursor.execute(
        "SELECT title, price, item_url FROM mercari_products "
        "WHERE status IN ('売却済み','販売履歴') ORDER BY id DESC LIMIT 100"
    )
    top_sold = sorted(cursor.fetchall(),
                      key=lambda r: _parse_price_int(r[1]), reverse=True)[:10]
    conn.close()

    def _ai_table(items, has_date=False):
        if not items:
            return ('<p style="font-size:13px;color:var(--muted);padding:10px 20px 14px">'
                    '該当商品なし</p>')
        rows = ""
        for item in items:
            title = html_module.escape(item[0] or "")
            price = html_module.escape(item[1] or "")
            url   = item[2] or ""
            link  = (f'<a class="link-btn open-link" href="#" '
                     f'data-url="{html_module.escape(url)}">開く ↗</a>'
                     if url else "")
            date_td = ""
            if has_date and len(item) > 3:
                date_td = (f'<td style="font-size:12px;color:var(--muted)">'
                           f'{html_module.escape(str(item[3] or ""))}</td>')
            rows += (f"<tr><td>{title}</td>"
                     f"<td class='price'>{price}</td>"
                     f"{date_td}"
                     f"<td>{link}</td></tr>")
        date_th = "<th>出品日</th>" if has_date else ""
        return (f'<div style="overflow-x:auto"><table><thead><tr>'
                f'<th data-sortable data-col="0">商品名</th>'
                f'<th data-sortable data-col="1">価格</th>'
                f'{date_th}<th>リンク</th>'
                f'</tr></thead><tbody>{rows}</tbody></table></div>')

    def _section(icon, title, count, tip, table_html):
        badge = (f'<span class="suggestion-badge">{count} 件</span>'
                 if count > 0 else "")
        return f"""
        <div class="suggestion-card">
          <div class="suggestion-header">
            <span class="suggestion-icon">{icon}</span>
            <span class="suggestion-title">{html_module.escape(title)}</span>
            {badge}
          </div>
          <p class="suggestion-tip">{tip}</p>
          {table_html}
        </div>"""

    sections = [
        _section(
            "🚫", "公開停止中商品", len(stopped_items),
            "出品が停止されています。再開するか、商品リストを整理することをお勧めします。",
            _ai_table(stopped_items),
        ),
        _section(
            "⏳", "長期出品中（60日以上）", len(long_items),
            "60日以上出品中の商品です。価格の見直し・商品説明の更新を検討してください。",
            _ai_table(long_items, has_date=True),
        ),
        _section(
            "💰", f"高価格帯（平均 ¥{avg_active:,} の2倍以上）", len(high_items),
            "平均価格の2倍以上の商品です。競合価格と比較し、適切な価格設定か確認してください。",
            _ai_table(high_items),
        ),
        _section(
            "📉", f"低価格帯（平均 ¥{avg_active:,} の50%以下）", len(low_items),
            "平均価格の半値以下の商品です。値上げの余地がある可能性があります。",
            _ai_table(low_items),
        ),
        _section(
            "🏆", "高売上商品 TOP 10", len(top_sold),
            "売却額上位の商品です。仕入れ・出品の参考にしてください。",
            _ai_table(top_sold),
        ),
    ]

    content = "\n".join(sections)
    return _page_shell("AI 分析", "ai", content, _SORT_JS,
                       subtitle="商品データに基づく改善提案")


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if _session_state != "valid":
        return redirect("/login")

    # Inline license key activation (POST from plan card form)
    if request.method == "POST" and request.args.get("action") == "activate":
        key = request.form.get("key", "").strip()
        plan_name, err = _validate_license_key(key)
        if err:
            import urllib.parse
            return redirect("/settings?key_err=" + urllib.parse.quote(err))
        state = _get_license()
        state["plan"]         = plan_name
        state["activated_at"] = datetime.now(_JST).isoformat()
        if plan_name == "monthly":
            expiry = datetime.now(_JST) + timedelta(days=_MONTHLY_DAYS)
            state["expiry_time"] = expiry.isoformat()
        else:
            state["expiry_time"] = None
        _save_license(state)
        return redirect("/settings?key_ok=1")

    _state_map = {
        "valid":   ("✓ ログイン済み", "#dcfce7", "#166534"),
        "invalid": ("✗ 未ログイン",   "#fee2e2", "#991b1b"),
    }
    state_label, badge_bg, badge_fg = _state_map.get(
        _session_state, (_session_state, "#f3f4f6", "#374151")
    )
    state_badge = (f'<span class="badge" style="background:{badge_bg};color:{badge_fg}">'
                   f'{html_module.escape(state_label)}</span>')

    app_data_dir = os.path.dirname(os.path.abspath(DB_NAME)) if DB_NAME else "不明"

    info_card = f"""
    <div class="card">
      <div class="card-header"><span class="card-title">セッション情報</span></div>
      <div class="card-body">
        <div class="settings-row">
          <span class="settings-label">ログイン状態</span>
          {state_badge}
        </div>
        <div class="settings-row">
          <span class="settings-label">最終ログイン</span>
          <span class="settings-value">
            {html_module.escape(_session_last_login or "不明")}
          </span>
        </div>
        <div class="settings-row">
          <span class="settings-label">Chromeプロファイル</span>
          <span class="settings-path">
            {html_module.escape(CHROME_PROFILE_DIR or "未設定")}
          </span>
        </div>
        <div class="settings-row">
          <span class="settings-label">データ保存先</span>
          <span class="settings-path">
            {html_module.escape(app_data_dir)}
          </span>
        </div>
      </div>
    </div>"""

    actions_card = """
    <div class="card">
      <div class="card-header"><span class="card-title">セッション操作</span></div>
      <div class="card-body">
        <div style="display:flex;flex-direction:column;gap:12px;max-width:360px">
          <form method="POST" action="/login/relogin">
            <button class="btn btn-outline" type="submit"
                    style="width:100%;justify-content:center">
              再ログイン（セッション更新）
            </button>
          </form>
          <form method="POST" action="/login/clear">
            <button class="btn btn-danger" type="submit"
                    style="width:100%;justify-content:center"
                    onclick="return confirm('ログインデータを削除しますか?\\nChromeプロファイルとCookieが削除されます。')">
              ログインデータを削除
            </button>
          </form>
        </div>
        <p style="margin-top:16px;font-size:12px;color:var(--muted)">
          &#9888; 削除されるのはアプリ専用のChromeプロファイルのみです。
          システムのChromeには影響しません。
        </p>
      </div>
    </div>"""

    # ── Inline activation flash messages ──────────────────────────────────
    key_ok  = request.args.get("key_ok") == "1"
    key_err = request.args.get("key_err", "")
    if key_ok:
        activate_flash = (
            '<div class="error-banner" '
            'style="background:#dcfce7;border-color:#86efac;color:#166534;margin-bottom:12px">'
            '&#10003; ライセンスが有効化されました。プロ機能が利用可能です。'
            '</div>'
        )
    elif key_err:
        import urllib.parse
        activate_flash = (
            f'<div class="error-banner" style="margin-bottom:12px">'
            f'<strong>エラー:</strong> {html_module.escape(urllib.parse.unquote(key_err))}'
            f'</div>'
        )
    else:
        activate_flash = ""

    # ── Plan info card ────────────────────────────────────────────────────
    lic          = _get_license()
    cur_plan     = _check_plan()
    days_left    = _trial_days_remaining()
    monthly_days = _monthly_days_remaining()
    expiry_str   = _monthly_expiry_str()
    activated_at = lic.get("activated_at") or "–"
    if activated_at and activated_at != "–":
        activated_at = activated_at[:16]

    plan_label_map = {
        "trial":    f"トライアル（残り {days_left} 日）",
        "expired":  "トライアル終了",
        "free":     "無料版",
        "monthly":  f"Pro 月額プラン（残り {monthly_days} 日 / {expiry_str} まで）",
        "lifetime": "Pro 買い切り（無期限）",
    }
    plan_label = plan_label_map.get(cur_plan, cur_plan)
    upgrade_link = (
        '<a href="/upgrade" class="btn btn-primary" '
        'style="padding:8px 16px;font-size:13px;text-decoration:none">アップグレード</a>'
        if cur_plan in ("trial", "expired", "free") else ""
    )

    plan_card = f"""
    <div class="card">
      <div class="card-header"><span class="card-title">プラン情報</span></div>
      <div class="card-body">
        {activate_flash}
        <div class="settings-row">
          <span class="settings-label">現在のプラン</span>
          <div style="display:flex;align-items:center;gap:12px">
            {_license_badge_html()}
            {upgrade_link}
          </div>
        </div>
        <div class="settings-row">
          <span class="settings-label">プラン詳細</span>
          <span class="settings-value">{html_module.escape(plan_label)}</span>
        </div>
        <div class="settings-row">
          <span class="settings-label">有効化日時</span>
          <span class="settings-value">{html_module.escape(str(activated_at))}</span>
        </div>
        <div class="settings-row" style="flex-direction:column;align-items:flex-start;gap:10px">
          <span class="settings-label">ライセンスキーを入力</span>
          <form method="POST" action="/settings?action=activate"
                style="display:flex;gap:10px;align-items:center;width:100%;max-width:480px">
            <input class="text-input" type="text" name="key"
                   placeholder="MIA-LIFE-XXXX-XXXX または MIA-MONTH-XXXX-XXXX"
                   style="font-family:monospace;letter-spacing:.05em;flex:1">
            <button class="btn btn-primary" type="submit"
                    style="white-space:nowrap">アクティベート</button>
          </form>
          <p style="font-size:12px;color:var(--muted)">
            キーをお持ちでない場合は
            <a href="/upgrade" style="color:var(--primary)">プラン選択ページ</a>へ。
          </p>
        </div>
      </div>
    </div>"""

    app_card = f"""
    <div class="card">
      <div class="card-header"><span class="card-title">アプリ情報</span></div>
      <div class="card-body">
        <div class="settings-row">
          <span class="settings-label">バージョン</span>
          <span class="settings-value">{APP_VERSION}</span>
        </div>
        <div class="settings-row">
          <span class="settings-label">免責事項</span>
          <span class="settings-value" style="font-size:12px;color:var(--muted);text-align:right;max-width:400px">
            本ツールは Mercari 販売者向けの独立した在庫管理ツールです。
            Mercari 株式会社との公式な提携・認定関係はありません。
          </span>
        </div>
      </div>
    </div>"""

    content = f"{info_card}\n{plan_card}\n{actions_card}\n{app_card}"
    return _page_shell("設定", "settings", content,
                       subtitle="セッション管理・アプリ情報")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_listing_text(text):
    """Extract title, price, created_at, status from a listing card's visible text.

    Returns a 4-tuple: (title, price, created_at, status).
    Uses _DETECT_STATUSES so 公開停止中 badges are captured even though it is
    not a sync target in STATUSES.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    price = ""
    title = ""
    created_at = ""
    status = ""

    for i, line in enumerate(lines):
        if line in _DETECT_STATUSES and not status:
            status = line

        if line == "¥" and i + 1 < len(lines) and lines[i + 1].replace(",", "").isdigit():
            price = "¥" + lines[i + 1]
        elif line.startswith("¥") and len(line) > 1 and not price:
            m = re.search(r"¥([\d,]+)", line)
            if m and int(m.group(1).replace(",", "")) > 0:
                price = "¥" + m.group(1)

        for kw in TIME_KEYWORDS:
            if kw in line:
                if "更新" in line or not created_at:
                    created_at = line
                break

    ignore = {"¥"} | INVALID_TITLES | _DETECT_STATUSES
    for line in reversed(lines):
        if line in ignore:
            continue
        if line.replace(",", "").isdigit():
            continue
        if any(kw in line for kw in TIME_KEYWORDS):
            continue
        if line.startswith("¥"):
            continue
        title = line
        break

    return title, price, created_at, status


def is_valid_title(title):
    return bool(title) and title not in INVALID_TITLES and not title.replace(",", "").isdigit()


def _is_valid_price(price: str) -> bool:
    """Return True when price contains an actual number (not just '¥' or empty)."""
    if not price:
        return False
    digits = re.sub(r"[¥,\s]", "", price)
    return digits.isdigit() and int(digits) > 0


def _clear_profile_lock() -> None:
    """Remove stale Chrome singleton lock files so a new instance can start.

    Chrome writes lock files (SingletonLock, SingletonCookie, SingletonSocket)
    into the user-data-dir. If Chrome crashes or is force-killed these remain
    and prevent a new Chrome process from using the same profile directory.
    """
    if not CHROME_PROFILE_DIR:
        return
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = os.path.join(CHROME_PROFILE_DIR, lock_name)
        try:
            if os.path.exists(lock_path) or os.path.islink(lock_path):
                os.remove(lock_path)
                print(f"[driver] プロファイルロック削除: {lock_name}")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

def wait_for_items(driver, timeout=15):
    """Block until at least one item/transaction link or table row is present."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']") or
                driver.find_elements(By.CSS_SELECTOR, "a[href*='/transaction/']") or
                driver.find_elements(By.CSS_SELECTOR, "table tr td")):
            return
        time.sleep(0.5)


def wait_for_count_increase(driver, previous_count, timeout=6):
    """Poll until collected item count exceeds previous_count."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = (len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")) +
             len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/transaction/']")))
        if n > previous_count:
            return
        time.sleep(0.4)


def _collect_from_table(driver, seen_urls):
    """Parse table/row-based listing pages (e.g., 販売履歴 sold page)."""
    items = []
    for row in driver.find_elements(By.CSS_SELECTOR, "tr"):
        row_link = None
        for a in row.find_elements(By.TAG_NAME, "a"):
            href = a.get_attribute("href") or ""
            if "/item/" in href or "/transaction/" in href:
                row_link = a
                break
        if not row_link:
            continue
        href = row_link.get_attribute("href")
        if href in seen_urls:
            continue
        row_text = row.text.strip()
        title, price, created_at, status = parse_listing_text(row_text) if row_text else ("", "", "", "")
        items.append({
            "url": href,
            "title": title or row_link.text.strip(),
            "price": price,
            "created_at": created_at,
            "status": status,
            "raw_text": row_text,
        })
        seen_urls.add(href)
    return items


def collect_items_from_page(driver, status_label=None):
    """Return all unique item links with listing-page metadata.

    Uses three strategies in order:
    1. Standard <a href="/item/..."> links (all listing pages)
    2. Table-row parser (for 販売履歴 sold page which uses table DOM)
    3. Transaction links <a href="/transaction/..."> (fallback)
    """
    seen_urls = set()
    items = []

    # Strategy 1: standard item links
    for a in driver.find_elements(By.TAG_NAME, "a"):
        href = a.get_attribute("href") or ""
        if "/item/" not in href or href in seen_urls:
            continue
        text = a.text.strip()
        title, price, created_at, status = parse_listing_text(text) if text else ("", "", "", "")
        items.append({
            "url": href,
            "title": title,
            "price": price,
            "created_at": created_at,
            "status": status,
            "raw_text": text,
        })
        seen_urls.add(href)

    if items:
        return items

    # Strategy 2: table rows (sold page DOM)
    table_items = _collect_from_table(driver, seen_urls)
    if table_items:
        return table_items

    # Strategy 3: transaction links (last resort)
    for a in driver.find_elements(By.TAG_NAME, "a"):
        href = a.get_attribute("href") or ""
        if "/transaction/" not in href or href in seen_urls:
            continue
        text = a.text.strip()
        title, price, created_at, status = parse_listing_text(text) if text else ("", "", "", "")
        items.append({
            "url": href,
            "title": title,
            "price": price,
            "created_at": created_at,
            "status": status,
            "raw_text": text,
        })
        seen_urls.add(href)

    return items


def find_more_button(driver):
    for el in driver.find_elements(By.TAG_NAME, "button") + driver.find_elements(By.TAG_NAME, "a"):
        if "もっと見る" in el.text or "もっとみる" in el.text:
            return el
    return None


def _find_next_button(driver):
    """Find an active Next/次へ pagination button (used by 販売履歴 table pages).

    Returns None if no button is found or if it is disabled.
    """
    for el in driver.find_elements(By.TAG_NAME, "button") + driver.find_elements(By.TAG_NAME, "a"):
        text = el.text.strip()
        if "次へ" not in text and "次のページ" not in text:
            continue
        disabled = (
            el.get_attribute("disabled") is not None
            or "disabled" in (el.get_attribute("class") or "").lower()
            or el.get_attribute("aria-disabled") == "true"
        )
        if not disabled:
            return el
    return None


def _log_empty_page(driver, status_label: str) -> None:
    """Log diagnostic details when a listing page yields no product cards.

    Helps distinguish a genuinely empty account page from a rendering failure
    (minimized viewport, login redirect, lazy-load timeout, DOM change).
    """
    print(f"[{status_label}] 商品が見つかりませんでした — 診断情報:")
    try:
        print(f"  URL    : {driver.current_url}")
        print(f"  Title  : {driver.title}")
        if "login" in driver.current_url.lower():
            print(f"  [警告] ログインページにリダイレクトされています — セッション切れの可能性")
        body_text = driver.find_element(By.TAG_NAME, "body").text
        snippet = " ".join(body_text.split())[:300]
        print(f"  Body先頭: {snippet}")
        item_links  = len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']"))
        trans_links = len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/transaction/']"))
        table_rows  = len(driver.find_elements(By.CSS_SELECTOR, "table tr td"))
        print(f"  /item/ リンク数: {item_links} | /transaction/ リンク数: {trans_links} | テーブル行: {table_rows}")
    except Exception as exc:
        print(f"  [診断取得エラー] {exc}")


def load_listings_for_status(driver, status_label, pagination_timeout=6):
    """Load all items from the Mercari page for one status, paginating fully.

    Sets item['status'] = status_label (URL-based, overrides card badge).
    For 出品中 pages, also sets item['visibility_status'] based on whether the
    card showed a 公開停止中 badge ('stopped') or not ('public').
    """
    url = STATUS_URLS.get(status_label)
    if not url:
        print(f"[{status_label}] 未知ステータス — スキップ")
        return []

    print(f"\n[{status_label}] {url} に遷移中...")
    driver.get(url)
    wait_for_items(driver, timeout=15)

    initial = collect_items_from_page(driver, status_label)
    if not initial:
        _log_empty_page(driver, status_label)
        return []

    # Detect page layout: table DOM → Next-button pagination (e.g. 販売履歴)
    #                    card DOM  → scroll + もっと見る pagination
    is_table_page = bool(driver.find_elements(By.CSS_SELECTOR, "table tr td"))

    if is_table_page:
        # ── Next-button pagination ─────────────────────────────────────────
        seen_urls = {i["url"] for i in initial}
        all_items = list(initial)
        for page_num in range(1, 51):  # safety cap: 50 pages
            if _sync_stop_requested:
                raise _SyncStopped()
            next_btn = _find_next_button(driver)
            if not next_btn:
                print(f"\n[{status_label}] 次へボタンなし — 全件読み込み完了 ({page_num} ページ)")
                break
            print(f"\n[{status_label}] 次へ クリック (ページ {page_num + 1})...", flush=True)
            driver.execute_script("arguments[0].click();", next_btn)
            time.sleep(2)
            wait_for_items(driver, timeout=10)
            page_items = collect_items_from_page(driver, status_label)
            new = [i for i in page_items if i["url"] not in seen_urls]
            if not new:
                print(f"\n[{status_label}] 新規アイテムなし — 終了")
                break
            seen_urls.update(i["url"] for i in new)
            all_items.extend(new)
            print(f"  [{status_label}] 累計: {len(all_items)} 件", flush=True)
        final = all_items
    else:
        # ── Scroll + もっと見る pagination ────────────────────────────────
        for click_num in range(200):
            if _sync_stop_requested:
                raise _SyncStopped()
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.8)

            current_items = collect_items_from_page(driver, status_label)
            print(f"  [{status_label}] 読み込み済み: {len(current_items)} 件", end="\r", flush=True)

            more_btn = find_more_button(driver)
            if not more_btn:
                print(f"\n[{status_label}] 全件読み込み完了")
                break

            prev_count = len(current_items)
            driver.execute_script("arguments[0].click();", more_btn)
            print(f"\n[{status_label}] 「もっと見る」クリック {click_num + 1} 回目...", flush=True)
            wait_for_count_increase(driver, prev_count, timeout=pagination_timeout)

        final = collect_items_from_page(driver, status_label)

    # Apply URL-based status; detect 公開停止中 sub-state for 出品中 items
    for item in final:
        badge_status = item.get("status", "")
        item["status"] = status_label
        if status_label == "出品中":
            item["visibility_status"] = "stopped" if badge_status == "公開停止中" else "public"
        else:
            item["visibility_status"] = ""

    stopped_count = sum(1 for i in final if i.get("visibility_status") == "stopped")
    print(f"[{status_label}] 取得完了: {len(final)} 件"
          + (f" (うち公開停止中: {stopped_count} 件)" if stopped_count else ""))
    return final


def load_all_listings(driver, selected_statuses):
    """Load items for every selected status page, deduplicating by URL."""
    all_items = []
    seen_urls = set()
    counts = {}

    _sync_progress["total_steps"] = len(selected_statuses)

    for idx, status in enumerate(selected_statuses, start=1):
        if _sync_stop_requested:
            raise _SyncStopped()

        _sync_progress["step"]     = status
        _sync_progress["step_num"] = idx

        timeout = 10 if status in _LONG_TIMEOUT_STATUSES else 6
        items = load_listings_for_status(driver, status, pagination_timeout=timeout)
        new_items = [i for i in items if i["url"] not in seen_urls]
        seen_urls.update(i["url"] for i in new_items)
        all_items.extend(new_items)
        counts[status] = len(new_items)

        _sync_progress["fetched"] = len(all_items)

    print("\n--- ステータス別取得件数 ---")
    for s in selected_statuses:
        print(f"  {s}: {counts.get(s, 0)}")
    print(f"  合計: {len(all_items)} 件")
    return all_items, counts


# ---------------------------------------------------------------------------
# Normalization helpers for comparison
# ---------------------------------------------------------------------------

def _norm_str(s) -> str:
    return (s or "").strip()

def _norm_price(s) -> str:
    return re.sub(r"\s+", "", s or "")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def fetch_existing_batch(urls):
    """Single query to fetch existing records for all given URLs."""
    if not urls:
        return {}
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(urls))
    cursor.execute(
        f"SELECT item_url, title, price, created_at, status, visibility_status "
        f"FROM mercari_products WHERE item_url IN ({placeholders})",
        urls,
    )
    result = {
        row[0]: {
            "title": row[1],
            "price": row[2],
            "created_at": row[3],
            "status": row[4] or "",
            "visibility_status": row[5] or "",
        }
        for row in cursor.fetchall()
    }
    conn.close()
    return result


def save_or_update_product(item_url, title, price, status, created_at, raw_text,
                           visibility_status=""):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title, price, status, created_at, visibility_status "
        "FROM mercari_products WHERE item_url = ?",
        (item_url,),
    )
    row = cursor.fetchone()

    if row:
        _, old_title, old_price, old_status, old_created_at, old_vis = row

        changes = []
        if _norm_str(old_title) != _norm_str(title):
            changes.append(f"title: {old_title!r} → {title!r}")
        if _norm_price(old_price) != _norm_price(price):
            changes.append(f"price: {old_price} → {price}")
        if _norm_str(old_status) != _norm_str(status):
            changes.append(f"status: {old_status} → {status}")
        if _norm_str(old_created_at) != _norm_str(created_at):
            changes.append(f"created_at: {old_created_at} → {created_at}")
        if _norm_str(old_vis) != _norm_str(visibility_status):
            changes.append(f"visibility_status: {old_vis} → {visibility_status}")

        if not changes:
            cursor.execute(
                "UPDATE mercari_products SET synced_at = ? WHERE item_url = ?",
                (jst_now(), item_url),
            )
            conn.commit()
            conn.close()
            return "skipped"

        print(f"  [更新理由] {', '.join(changes)} — {item_url}")
        cursor.execute("""
            UPDATE mercari_products
            SET title = ?, price = ?, status = ?, created_at = ?, raw_text = ?,
                synced_at = ?, visibility_status = ?
            WHERE item_url = ?
        """, (title, price, status, created_at, raw_text, jst_now(), visibility_status, item_url))
        conn.commit()
        conn.close()
        return "updated"

    cursor.execute("""
        INSERT INTO mercari_products
            (item_url, title, price, status, created_at, raw_text, synced_at, visibility_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (item_url, title, price, status, created_at, raw_text, jst_now(), visibility_status))
    conn.commit()
    conn.close()
    return "inserted"


def touch_synced_at(item_url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE mercari_products SET synced_at = ? WHERE item_url = ?",
        (jst_now(), item_url),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Detail-page scraping
# ---------------------------------------------------------------------------

def scrape_item_detail(driver, url):
    """Open a product detail page and extract title, price, raw_text."""
    item_id = url.rstrip("/").split("/")[-1]
    title = ""
    price = ""
    raw_text = ""

    for attempt in range(MAX_RETRY + 1):
        if attempt > 0:
            print(f"Retry {attempt} for item {item_id}")
            time.sleep(2)

        driver.get(url)
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.TAG_NAME, "h1"))
            )
        except Exception:
            pass

        raw_text = driver.find_element(By.TAG_NAME, "body").text
        title = ""
        price = ""

        h1s = driver.find_elements(By.TAG_NAME, "h1")
        if h1s:
            title = h1s[0].text.strip()

        for line in (l.strip() for l in raw_text.split("\n") if l.strip()):
            m = re.search(r"¥([\d,]+)", line)
            if m and int(m.group(1).replace(",", "")) > 0:
                price = "¥" + m.group(1)
                break

        if price:
            break

    if not price:
        print(f"WARNING: Price missing after retries for item {item_id}")

    return title, price, raw_text


# ---------------------------------------------------------------------------
# Parallel detail fetching
# ---------------------------------------------------------------------------

def _make_chrome_driver(headless=False) -> "webdriver.Chrome":
    """Create a configured Chrome driver.

    Visible driver (headless=False):
    - Uses persistent profile at CHROME_PROFILE_DIR so Mercari session cookies
      are stored natively and survive across app restarts.
    - Images are allowed (needed for product-page viewing).

    Headless workers (headless=True):
    - Fresh profile each time (no --user-data-dir).
    - Images blocked to speed up detail-page scraping.

    Anti-detection flags are applied to all instances.
    """
    opts = Options()

    # ── Anti-detection ────────────────────────────────────────────────────
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        # Block images only for headless scraping workers (not for product viewing)
        opts.add_experimental_option("prefs", {
            "profile.managed_default_content_settings.images": 2
        })
    else:
        opts.add_argument("--start-maximized")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        # Persistent profile: Mercari session and cookies survive across runs
        if CHROME_PROFILE_DIR:
            os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
            opts.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
            opts.add_argument("--profile-directory=Default")

    # Ensure Selenium Manager binary is found and executable before first use.
    # PyInstaller's collect_data_files() does not preserve the +x bit; this
    # call fixes that and sets SE_MANAGER_PATH so Selenium always finds it.
    _ensure_selenium_manager()

    # Attempt to start Chrome; retry once with extra cleanup for visible driver.
    max_attempts = 2 if not headless else 1
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            driver = webdriver.Chrome(options=opts)
            break
        except Exception as exc:
            last_exc = exc
            print(f"[driver] Chrome起動失敗 (attempt {attempt + 1}/{max_attempts}): {exc}")
            if attempt == 0 and not headless:
                # First failure on visible driver: kill orphans, clear locks, retry
                _kill_orphan_chromedriver()
                _clear_profile_lock()
                time.sleep(1.0)
    else:
        raise RuntimeError(
            "Chrome の自動ドライバーセットアップに失敗しました。\n"
            "Google Chrome がインストールされていることを確認してください。\n"
            "https://www.google.com/chrome/"
        ) from last_exc

    # Remove navigator.webdriver fingerprint so Mercari does not detect Selenium
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
    except Exception:
        pass  # CDP may not be available for some headless configurations

    return driver


def build_driver_pool(n, seed_cookies=None):
    pool = queue.Queue()
    for i in range(n):
        driver = _make_chrome_driver(headless=True)
        if seed_cookies:
            driver.get("https://jp.mercari.com")
            for cookie in seed_cookies:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
        pool.put(driver)
        print(f"  worker {i + 1}/{n} 初始化完成", flush=True)
    return pool


def make_pool_fetcher(driver_pool):
    def fetch_item_detail(url):
        driver = driver_pool.get()
        try:
            title, price, raw_text = scrape_item_detail(driver, url)
            return {"url": url, "title": title, "price": price,
                    "raw_text": raw_text, "error": None}
        except Exception as exc:
            return {"url": url, "title": "", "price": "",
                    "raw_text": "", "error": str(exc)}
        finally:
            driver_pool.put(driver)

    return fetch_item_detail


# ---------------------------------------------------------------------------
# Item classification
# ---------------------------------------------------------------------------

def classify_items(items, existing_map):
    """Split items into to_skip / to_save_direct / to_fetch_detail."""
    to_skip = []
    to_save_direct = []
    to_fetch_detail = []

    for item in items:
        existing = existing_map.get(item["url"])
        if existing:
            old_title  = existing["title"] or ""
            old_cat    = existing["created_at"] or ""
            old_status = existing.get("status") or ""
            old_vis    = existing.get("visibility_status") or ""
            old_price  = existing.get("price") or ""
            new_status = item.get("status") or ""
            new_vis    = item.get("visibility_status") or ""
            new_price  = item.get("price") or ""
            # Skip when all meaningful fields match (normalized) and price is valid
            if (is_valid_title(_norm_str(old_title))
                    and _norm_str(old_cat) == _norm_str(item["created_at"])
                    and _norm_str(old_status) == _norm_str(new_status)
                    and _norm_str(old_vis) == _norm_str(new_vis)
                    and _norm_price(old_price) == _norm_price(new_price)
                    and _is_valid_price(old_price)):
                to_skip.append(item)
                continue

        if is_valid_title(item["title"]) and _is_valid_price(item.get("price", "")):
            to_save_direct.append(item)
        else:
            to_fetch_detail.append(item)

    return to_skip, to_save_direct, to_fetch_detail


# ---------------------------------------------------------------------------
# Login helper
# ---------------------------------------------------------------------------

def click_login_button_if_exists(driver):
    time.sleep(2)
    for el in driver.find_elements(By.TAG_NAME, "button") + driver.find_elements(By.TAG_NAME, "a"):
        text = el.text.strip()
        if "ログイン" in text or "login" in text.lower():
            try:
                driver.execute_script("arguments[0].click();", el)
                print("ログイン按钮已自动点击")
                time.sleep(1)
                return
            except Exception:
                pass
    print("没有找到可自动点击的ログイン按钮，请手动点击。")


def wait_for_login(driver, timeout=300):
    """Poll until the browser URL leaves the login page."""
    print("ブラウザで Mercari にログインしてください。")
    print("ログイン完了後、自動的に同期を開始します（最大 5 分待機）...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if "login" not in driver.current_url:
                time.sleep(1)
                print("ログイン確認完了。同期を開始します...")
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError("ログインがタイムアウトしました（5 分）。アプリを再起動してください。")


def _check_session_background() -> None:
    """Check Mercari session validity in a background thread.

    Sets _session_state to "found_session" (session exists — user must choose)
    or "invalid" (no session — show login button).
    Never auto-transitions to "valid"; that requires an explicit user action.
    """
    global _session_state, _session_last_login
    try:
        driver = _get_or_create_driver()
        if _try_restore_session(driver):
            # Populate last-login time from cookie file modification time
            if COOKIE_FILE and os.path.exists(COOKIE_FILE):
                try:
                    mtime = os.path.getmtime(COOKIE_FILE)
                    _session_last_login = datetime.fromtimestamp(mtime, tz=_JST).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                except Exception:
                    _session_last_login = jst_now()[:16]
            _session_state = "found_session"
        else:
            _session_state = "invalid"
    except Exception as exc:
        print(f"[session] セッションチェック失敗: {exc}")
        _session_state = "invalid"


def _do_login(force_relogin: bool = False) -> None:
    """Perform interactive Mercari login in a background thread.

    Brings Chrome to screen, navigates to the login page, waits for the user
    to complete login, saves cookies, then moves Chrome off-screen.
    Sets _session_state to "valid" on success, "invalid" on failure/timeout.

    force_relogin=True: deletes browser cookies before opening the login page
    so the existing session cannot silently bypass the login form.
    """
    global _session_state
    try:
        driver = _get_or_create_driver()
        try:
            driver.maximize_window()
            driver.set_window_position(0, 0)
        except Exception:
            pass

        if force_relogin:
            # Wipe in-session cookies so Chrome doesn't auto-restore the old login.
            # delete_all_cookies() flushes through to the profile's SQLite DB.
            try:
                driver.get("https://jp.mercari.com")
                driver.delete_all_cookies()
                time.sleep(0.5)
            except Exception:
                pass

        driver.get("https://jp.mercari.com/login")
        click_login_button_if_exists(driver)
        wait_for_login(driver)
        _save_session_cookies(driver)
        try:
            driver.set_window_position(-3000, 0)
        except Exception:
            pass
        _session_state = "valid"
        print("[login] ログイン完了 — セッション有効")
    except Exception as exc:
        print(f"[login] ログイン失敗: {exc}")
        _session_state = "invalid"


def _clear_session() -> None:
    """Delete all stored session data and reset to logged-out state.

    Safety: only deletes CHROME_PROFILE_DIR when it is inside the app's own
    Application Support directory — never touches the system Chrome profile.
    """
    global _singleton_driver, _session_state, _session_last_login

    # Quit the singleton driver before touching the profile directory.
    # Chrome holds a lock on the profile; deleting while it's open corrupts it.
    with _driver_lock:
        if _singleton_driver is not None:
            try:
                _singleton_driver.quit()
            except Exception:
                pass
            _singleton_driver = None
    _kill_orphan_chromedriver()
    time.sleep(0.5)   # let Chrome release file handles

    # Delete JSON cookie backup
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        try:
            os.remove(COOKIE_FILE)
            print(f"[session] クッキーファイルを削除しました: {COOKIE_FILE}")
        except OSError as exc:
            print(f"[session] クッキーファイル削除失敗: {exc}")

    # Delete the app-specific Chrome profile directory.
    # Guard: the path must be inside ~/Library/Application Support/MIAInventory/
    _app_support = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "MIAInventory"
    )
    if (CHROME_PROFILE_DIR
            and os.path.isdir(CHROME_PROFILE_DIR)
            and os.path.realpath(CHROME_PROFILE_DIR).startswith(
                os.path.realpath(_app_support)
            )):
        import shutil
        try:
            shutil.rmtree(CHROME_PROFILE_DIR, ignore_errors=False)
            print(f"[session] Chromeプロファイルを削除しました: {CHROME_PROFILE_DIR}")
        except Exception as exc:
            print(f"[session] Chromeプロファイル削除失敗: {exc}")
    elif CHROME_PROFILE_DIR:
        print(f"[session] 安全チェック: プロファイルパスがアプリ外のため削除をスキップ: {CHROME_PROFILE_DIR}")

    _session_last_login = ""
    _session_state = "invalid"
    print("[session] セッションデータを削除しました — ログアウト状態")


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def run_scraper(selected_statuses=None):
    global _last_sync_summary, _session_state

    if selected_statuses is None:
        selected_statuses = list(STATUSES)

    start_jst = jst_now()
    sync_start = time.time()

    # ------------------------------------------------------------------
    # Phase 1: session check, collect all listings
    # The singleton Chrome stays alive after sync for product-link opening.
    # ------------------------------------------------------------------
    if _session_state != "valid":
        raise RuntimeError(
            "セッションが無効です。ログイン画面からログインし直してください。"
        )

    main_driver = _get_or_create_driver()

    # Re-verify session is still live (cookies may have expired since login check)
    if not _try_restore_session(main_driver):
        _session_state = "invalid"
        raise RuntimeError(
            "Mercari セッションが切れました。ログイン画面から再度ログインしてください。"
        )

    # Move Chrome off-screen so it doesn't stay in the foreground.
    # Do NOT minimize — a minimized window collapses the viewport to ~0,
    # which prevents Intersection Observer from firing and breaks lazy-loading
    # on card-based pages (出品中, 取引中, 売却済み).  We first maximize to
    # ensure a valid viewport (un-minimizes if user collapsed it manually),
    # then move the window off the visible screen area.
    try:
        main_driver.maximize_window()
        main_driver.set_window_position(-3000, 0)
    except Exception:
        pass

    phase1_start = time.time()
    items, per_status_counts = load_all_listings(main_driver, selected_statuses)
    total_count = len(items)

    existing_map = fetch_existing_batch([item["url"] for item in items])
    to_skip, to_save_direct, to_fetch_detail = classify_items(items, existing_map)

    print(f"  跳过（未変化）：{len(to_skip)} 件 | "
          f"列表页直接保存：{len(to_save_direct)} 件 | "
          f"需打开详情页：{len(to_fetch_detail)} 件")

    seed_cookies = main_driver.get_cookies()
    # Do NOT quit main_driver — it is the singleton and stays alive for
    # product-link opening between syncs.

    phase1_elapsed = time.time() - phase1_start

    # Build lookup maps for all items
    visibility_status_map = {item["url"]: item.get("visibility_status", "") for item in items}
    created_at_map        = {item["url"]: item["created_at"] for item in items}
    status_map            = {item["url"]: item.get("status", "") for item in items}

    for item in to_skip:
        touch_synced_at(item["url"])

    direct_inserted = direct_updated = direct_skipped = 0
    for item in to_save_direct:
        r = save_or_update_product(
            item["url"], item["title"], item["price"],
            item.get("status", ""), item["created_at"], item["raw_text"],
            item.get("visibility_status", ""),
        )
        if r == "inserted":
            direct_inserted += 1
        elif r == "updated":
            direct_updated += 1
        else:
            direct_skipped += 1

    # ------------------------------------------------------------------
    # Phase 2: parallel detail fetches
    # ------------------------------------------------------------------
    if _sync_stop_requested:
        raise _SyncStopped()

    detail_inserted = detail_updated = detail_skipped = detail_errors = 0
    phase2_elapsed = 0.0

    if to_fetch_detail:
        phase2_start = time.time()
        print(f"\n初始化 {MAX_WORKERS} 个并行 worker...")
        pool = build_driver_pool(MAX_WORKERS, seed_cookies)

        fetch_item_detail = make_pool_fetcher(pool)
        urls_to_fetch = [item["url"] for item in to_fetch_detail]

        print(f"开始并行抓取 {len(urls_to_fetch)} 个详情页（{MAX_WORKERS} workers）...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            results = list(executor.map(fetch_item_detail, urls_to_fetch))

        while not pool.empty():
            pool.get().quit()

        for r in results:
            if r["error"]:
                detail_errors += 1
                print(f"  [ERROR] {r['url']}: {r['error']}")
                continue
            result = save_or_update_product(
                r["url"], r["title"], r["price"],
                status_map[r["url"]], created_at_map[r["url"]], r["raw_text"],
                visibility_status_map.get(r["url"], ""),
            )
            if result == "inserted":
                detail_inserted += 1
                print(f"  新増：{r['title']} / {r['price']}")
            elif result == "updated":
                detail_updated += 1
                print(f"  更新：{r['title']} / {r['price']}")
            else:
                detail_skipped += 1

        phase2_elapsed = time.time() - phase2_start

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total_inserted = direct_inserted + detail_inserted
    total_updated  = direct_updated  + detail_updated
    total_skipped  = len(to_skip) + direct_skipped + detail_skipped
    total_elapsed  = time.time() - sync_start

    # DB counts by status — confirms what is searchable after sync
    _db_conn = sqlite3.connect(DB_NAME)
    _db_cur  = _db_conn.cursor()
    _db_cur.execute(
        "SELECT status, COUNT(*) FROM mercari_products GROUP BY status ORDER BY COUNT(*) DESC"
    )
    db_counts_by_status = dict(_db_cur.fetchall())
    _db_cur.execute("SELECT COUNT(*) FROM mercari_products")
    db_total = _db_cur.fetchone()[0]
    _db_conn.close()

    print(f"\n{'=' * 56}")
    print(f"  同期完了")
    print(f"  検出合計：        {total_count:>4} 件")
    print(f"  新増：            {total_inserted:>4} 件")
    print(f"  更新：            {total_updated:>4} 件")
    print(f"  スキップ：        {total_skipped:>4} 件")
    print(f"  詳細取得：        {len(to_fetch_detail):>4} 件"
          f"  ({MAX_WORKERS} 並列 worker)")
    if detail_errors:
        print(f"  取得失敗：        {detail_errors:>4} 件")
    print(f"  Phase1（一覧）：  {phase1_elapsed:>6.1f} 秒")
    if to_fetch_detail:
        print(f"  Phase2（詳細）：  {phase2_elapsed:>6.1f} 秒")
    print(f"  合計時間：        {total_elapsed:>6.1f} 秒")
    print(f"\n  --- DB ステータス別件数 ---")
    for _s, _c in db_counts_by_status.items():
        print(f"    {_s}: {_c}")
    print(f"    合計: {db_total}")
    print(f"{'=' * 56}")

    _last_sync_summary = {
        "start_jst":    start_jst,
        "end_jst":      jst_now(),
        "elapsed":      round(total_elapsed, 1),
        "per_status":   per_status_counts,
        "inserted":     total_inserted,
        "updated":      total_updated,
        "skipped":      total_skipped,
        "total":        total_count,
        "db_by_status": db_counts_by_status,
        "db_total":     db_total,
    }
    # Do NOT call webbrowser.open here — the sync() route already redirects to /?summary=1


if __name__ == "__main__":
    init_db()
    webbrowser.open("http://127.0.0.1:5050")
    app.run(debug=False)
