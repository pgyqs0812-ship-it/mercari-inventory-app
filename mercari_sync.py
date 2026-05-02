import os
import re
import queue
import time
import webbrowser
import sqlite3
import html as html_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from flask import Flask, redirect, request
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

DB_NAME = "products.db"
MAX_WORKERS = 4
MAX_RETRY = 3

_JST = timezone(timedelta(hours=9))


def jst_now() -> str:
    """Return the current time in JST as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now(tz=_JST).strftime("%Y-%m-%d %H:%M:%S")

app = Flask(__name__)

TIME_KEYWORDS = [
    "秒前", "分前", "時間前", "日前",
    "ヶ月前", "か月前", "年前",
    "半年前", "半年以上前",
]

INVALID_TITLES = {"公開停止中", "出品停止中", "売却済み", "出品中", "取引中", "名称未取得"}

STATUSES = ["出品中", "取引中", "売却済み", "公開停止中"]


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
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Flask UI
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT title, price, item_url, created_at, synced_at, status
        FROM mercari_products
        ORDER BY id DESC
    """)
    products = cursor.fetchall()
    conn.close()

    # Group products by status for tab display
    grouped = {s: [] for s in STATUSES}
    grouped[""] = []  # products with no detected status
    for p in products:
        s = p[5] or ""
        grouped.setdefault(s, []).append(p)

    # Build rows HTML for each status tab
    def build_rows(rows_list):
        html = ""
        for i, p in enumerate(rows_list, start=1):
            title = html_module.escape(p[0] or "名称未取得")
            price = html_module.escape(p[1] or "-")
            url = html_module.escape(p[2] or "")
            created_at = html_module.escape(p[3] or "-")
            synced_at = html_module.escape(p[4] or "-")
            status = html_module.escape(p[5] or "-")
            html += f"""
            <tr>
                <td>{i}</td>
                <td>{title}</td>
                <td class="price">{price}</td>
                <td class="status-cell">{status}</td>
                <td>{created_at}</td>
                <td>{synced_at}</td>
                <td><a href="{url}" target="_blank">打开</a></td>
            </tr>"""
        return html

    # Build tab buttons and panels
    visible_tabs = [s for s in STATUSES if grouped.get(s)]
    if not visible_tabs:
        visible_tabs = STATUSES  # show all tabs even if empty

    tab_buttons = ""
    tab_panels = ""
    first = True
    for s in STATUSES:
        count = len(grouped.get(s, []))
        active_btn = " active" if first else ""
        active_panel = " active" if first else ""
        tab_buttons += f'<button class="tab-btn{active_btn}" onclick="showTab(\'{s}\')" id="btn-{s}">{s}（{count}件）</button>\n'
        rows_html = build_rows(grouped.get(s, []))
        tab_panels += f"""
        <div id="tab-{s}" class="tab-panel{active_panel}">
            <table>
                <tr>
                    <th>No.</th><th>商品名</th><th>价格</th><th>ステータス</th>
                    <th>商品登录时间</th><th>抓取时间</th><th>链接</th>
                </tr>
                {rows_html}
            </table>
        </div>"""
        first = False

    # Status checkboxes for sync form
    checkboxes = ""
    for s in STATUSES:
        checkboxes += f'<label class="cb-label"><input type="checkbox" name="statuses" value="{s}" checked> {s}</label>\n'

    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Mercari库存管理</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                   background: #f5f6f8; padding: 30px; color: #222; }}
            h1 {{ font-size: 28px; margin-bottom: 16px; }}
            .sync-box {{ background: white; border-radius: 12px; padding: 20px 24px;
                         box-shadow: 0 2px 8px rgba(0,0,0,0.07); margin-bottom: 24px; }}
            .sync-box h2 {{ font-size: 15px; margin: 0 0 12px; color: #444; }}
            .cb-row {{ display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 14px; }}
            .cb-label {{ display: flex; align-items: center; gap: 6px; font-size: 14px;
                         cursor: pointer; user-select: none; }}
            .cb-label input {{ width: 16px; height: 16px; cursor: pointer; }}
            button.sync-btn {{ background: #222; color: white; border: none; border-radius: 8px;
                      padding: 10px 22px; font-size: 14px; cursor: pointer; }}
            button.sync-btn:hover {{ background: #444; }}
            .summary {{ color: #666; font-size: 13px; margin-bottom: 16px; }}
            .tabs {{ display: flex; gap: 4px; margin-bottom: 0; flex-wrap: wrap; }}
            .tab-btn {{ background: #e0e0e0; color: #555; border: none; border-radius: 8px 8px 0 0;
                        padding: 9px 18px; font-size: 13px; cursor: pointer; transition: background 0.15s; }}
            .tab-btn:hover {{ background: #ccc; }}
            .tab-btn.active {{ background: #222; color: white; }}
            .tab-panel {{ display: none; }}
            .tab-panel.active {{ display: block; }}
            table {{ width: 100%; border-collapse: collapse; background: white;
                     border-radius: 0 8px 8px 8px; overflow: hidden;
                     box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
            th {{ background: #222; color: white; text-align: left;
                  padding: 11px 12px; font-size: 13px; }}
            td {{ padding: 11px 12px; border-bottom: 1px solid #eee;
                  font-size: 13px; vertical-align: top; }}
            tr:hover {{ background: #f9f9f9; }}
            .price {{ font-weight: bold; color: #d32f2f; white-space: nowrap; }}
            .status-cell {{ white-space: nowrap; }}
            a {{ color: #0066cc; font-weight: 600; text-decoration: none; }}
        </style>
    </head>
    <body>
        <h1>Mercari库存管理</h1>
        <div class="sync-box">
            <h2>同期するステータスを選択</h2>
            <form method="POST" action="/sync">
                <div class="cb-row">
                    {checkboxes}
                </div>
                <button class="sync-btn" type="submit">同步Mercari商品</button>
            </form>
        </div>
        <div class="summary">DB 合計：{len(products)} 件</div>
        <div class="tabs">
            {tab_buttons}
        </div>
        {tab_panels}
        <script>
            function showTab(name) {{
                document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('tab-' + name).classList.add('active');
                document.getElementById('btn-' + name).classList.add('active');
            }}
        </script>
    </body>
    </html>
    """


@app.route("/sync", methods=["POST"])
def sync():
    selected = request.form.getlist("statuses")
    if not selected:
        selected = list(STATUSES)
    run_scraper(selected_statuses=selected)
    return redirect("/")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_listing_text(text):
    """Extract title, price, created_at, status from a listing-page link's visible text.

    Mercari renders each listing card as a single <a> element whose .text
    contains all visible fields separated by newlines.
    Returns a 4-tuple: (title, price, created_at, status).
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    price = ""
    title = ""
    created_at = ""
    status = ""

    for i, line in enumerate(lines):
        # Status label (appears as a badge on the card)
        if line in STATUSES and not status:
            status = line

        # Price: either "¥1,234" on one line, or "¥" followed by digits
        if line == "¥" and i + 1 < len(lines) and lines[i + 1].replace(",", "").isdigit():
            price = "¥" + lines[i + 1]
        elif line.startswith("¥") and not price:
            price = line

        # created_at: a line containing a relative time keyword
        for kw in TIME_KEYWORDS:
            if kw in line:
                # Prefer lines that also say "更新" (last-updated marker)
                if "更新" in line or not created_at:
                    created_at = line
                break

    ignore = {"¥", "公開停止中", "出品中", "取引中", "売却済み", "出品停止中"}
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


# ---------------------------------------------------------------------------
# Selenium helpers
# ---------------------------------------------------------------------------

def get_target_count(driver):
    """Parse the total listing count from the page using regex for reliability."""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return None

    matches = re.findall(r"(\d[\d,]*)\s*件", body_text)
    if matches:
        return max(int(m.replace(",", "")) for m in matches)
    return None


def wait_for_items(driver, timeout=10):
    """Block until at least one item link is present, or timeout."""
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/item/']"))
        )
    except Exception:
        pass


def wait_for_count_increase(driver, previous_count, timeout=6):
    """Poll until the number of item links on the page exceeds previous_count."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/item/']")) > previous_count:
            return
        time.sleep(0.4)


def collect_items_from_page(driver):
    """Return all unique item links with listing-page metadata (title, price, created_at)."""
    seen_urls = set()
    items = []

    for a in driver.find_elements(By.TAG_NAME, "a"):
        href = a.get_attribute("href")
        if not href or "/item/" not in href or href in seen_urls:
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


def load_all_listings(driver):
    """Navigate to the listings page and paginate via 'もっと見る' until all items load."""
    driver.get("https://jp.mercari.com/mypage/listings")
    print("正在进入出品列表页面...")

    wait_for_items(driver, timeout=10)

    target_count = get_target_count(driver)
    if target_count:
        print(f"目标商品数量：{target_count} 件")
    else:
        print("未能检测到目标商品数量，加载直到没有「もっと見る」为止")

    for click_num in range(50):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.8)

        current_items = collect_items_from_page(driver)
        print(f"  已加载：{len(current_items)} 件", end="\r", flush=True)

        if target_count and len(current_items) >= target_count:
            print(f"\n已达到目标商品数 {target_count}，停止加载")
            break

        more_btn = find_more_button(driver)
        if not more_btn:
            print(f"\n没有「もっと見る」按钮，加载完毕")
            break

        prev_count = len(current_items)
        driver.execute_script("arguments[0].click();", more_btn)
        print(f"\n第 {click_num + 1} 次点击「もっと見る」...", flush=True)
        wait_for_count_increase(driver, prev_count, timeout=6)

    final = collect_items_from_page(driver)
    print(f"最终取得商品链接：{len(final)} 件")
    return final


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
        f"SELECT item_url, title, price, created_at, status FROM mercari_products "
        f"WHERE item_url IN ({placeholders})",
        urls,
    )
    result = {row[0]: {"title": row[1], "price": row[2], "created_at": row[3], "status": row[4] or ""}
              for row in cursor.fetchall()}
    conn.close()
    return result


def save_or_update_product(item_url, title, price, status, created_at, raw_text):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM mercari_products WHERE item_url = ?", (item_url,))
    exists = cursor.fetchone()

    if exists:
        cursor.execute("""
            UPDATE mercari_products
            SET title = ?, price = ?, status = ?, created_at = ?, raw_text = ?,
                synced_at = ?
            WHERE item_url = ?
        """, (title, price, status, created_at, raw_text, jst_now(), item_url))
        conn.commit()
        conn.close()
        return "updated"

    cursor.execute("""
        INSERT INTO mercari_products (item_url, title, price, status, created_at, raw_text, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (item_url, title, price, status, created_at, raw_text, jst_now()))
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
    """Open a product detail page and extract title, price, raw_text.

    Retries up to MAX_RETRY times (with a 2-second pause between attempts)
    when price is missing. Logs each retry and a final warning if price
    cannot be found after all attempts.
    """
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
            if line.startswith("¥"):
                price = line
                break

        if price:
            break

    if not price:
        print(f"WARNING: Price missing after retries for item {item_id}")

    return title, price, raw_text


# ---------------------------------------------------------------------------
# Parallel detail fetching
# ---------------------------------------------------------------------------

def _make_chrome_driver(headless=False):
    """Create a configured Chrome driver. Workers use headless mode."""
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,900")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
    else:
        opts.add_argument("--start-maximized")
    opts.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2
    })

    # Selenium 4 prefers a chromedriver found in PATH over Selenium Manager.
    # A PATH-resident chromedriver (e.g. from Homebrew) is often a different
    # version than the installed Chrome and causes SessionNotCreatedException.
    # Temporarily removing chromedriver directories from PATH forces Selenium
    # Manager to download and cache the exactly matching driver version.
    original_path = os.environ.get("PATH", "")
    clean_path = os.pathsep.join(
        d for d in original_path.split(os.pathsep)
        if d and not os.path.isfile(os.path.join(d, "chromedriver"))
    )
    os.environ["PATH"] = clean_path
    try:
        return webdriver.Chrome(options=opts)
    except Exception as exc:
        raise RuntimeError(
            "Chrome の自動ドライバーセットアップに失敗しました。\n"
            "Google Chrome がインストールされていることを確認してください。\n"
            "https://www.google.com/chrome/"
        ) from exc
    finally:
        os.environ["PATH"] = original_path


def build_driver_pool(n, seed_cookies=None):
    """
    Create a pool of n headless Chrome drivers.

    If seed_cookies is provided (grabbed from the logged-in main driver),
    each worker navigates to the Mercari domain once to apply them so the
    session is shared across workers.
    """
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
    """
    Return fetch_item_detail(url) — a closure that checks out a driver
    from the pool, scrapes the detail page, then returns the driver.

    Safe for concurrent use: each call holds exactly one driver at a time.
    """
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
    """
    Split items into three buckets:

    to_skip        — DB has a valid title and created_at matches; no fetch needed.
    to_save_direct — Listing page already provides valid title + price; no detail page needed.
    to_fetch_detail — Data is incomplete; must open the detail page.
    """
    to_skip = []
    to_save_direct = []
    to_fetch_detail = []

    for item in items:
        existing = existing_map.get(item["url"])
        if existing:
            old_title = existing["title"] or ""
            old_created_at = existing["created_at"] or ""
            old_status = existing.get("status") or ""
            new_status = item.get("status") or ""
            if is_valid_title(old_title) and old_created_at == item["created_at"] and old_status == new_status:
                to_skip.append(item)
                continue

        if is_valid_title(item["title"]) and item["price"]:
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
    """Poll until the browser URL leaves the login page (user completed login).

    Replaces the old input() prompt — no Enter key needed. Times out after
    `timeout` seconds (default 5 minutes).
    """
    print("ブラウザで Mercari にログインしてください。")
    print("ログイン完了後、自動的に同期を開始します（最大 5 分待機）...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if "login" not in driver.current_url:
                time.sleep(1)  # let post-login redirect settle
                print("ログイン確認完了。同期を開始します...")
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError("ログインがタイムアウトしました（5 分）。アプリを再起動してください。")


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def run_scraper(selected_statuses=None):
    if selected_statuses is None:
        selected_statuses = list(STATUSES)

    sync_start = time.time()

    # ------------------------------------------------------------------
    # Phase 1: login with the main (visible) driver, collect all listings
    # ------------------------------------------------------------------
    main_driver = _make_chrome_driver(headless=False)
    main_driver.get("https://jp.mercari.com/login")
    click_login_button_if_exists(main_driver)
    wait_for_login(main_driver)

    phase1_start = time.time()
    items = load_all_listings(main_driver)
    print(f"\n共检测到商品：{len(items)} 件（全ステータス）")

    # Filter to only the statuses the user selected.
    # Items without a detected status are kept to avoid silently dropping them.
    items = [
        item for item in items
        if not item.get("status") or item["status"] in selected_statuses
    ]
    total_count = len(items)
    print(f"フィルタ後：{total_count} 件（選択ステータス: {', '.join(selected_statuses)}）")

    existing_map = fetch_existing_batch([item["url"] for item in items])
    to_skip, to_save_direct, to_fetch_detail = classify_items(items, existing_map)

    print(f"  跳过（未变化）：{len(to_skip)} 件 | "
          f"列表页直接保存：{len(to_save_direct)} 件 | "
          f"需打开详情页：{len(to_fetch_detail)} 件")

    # Capture session before quitting the main driver
    seed_cookies = main_driver.get_cookies()
    main_driver.quit()

    phase1_elapsed = time.time() - phase1_start

    # Apply skip (just touch synced_at) and direct saves
    for item in to_skip:
        touch_synced_at(item["url"])

    direct_inserted = direct_updated = 0
    for item in to_save_direct:
        r = save_or_update_product(
            item["url"], item["title"], item["price"],
            item.get("status", ""), item["created_at"], item["raw_text"]
        )
        if r == "inserted":
            direct_inserted += 1
        else:
            direct_updated += 1

    # ------------------------------------------------------------------
    # Phase 2: parallel detail fetches for items with incomplete data
    # ------------------------------------------------------------------
    detail_inserted = detail_updated = detail_errors = 0
    phase2_elapsed = 0.0

    if to_fetch_detail:
        phase2_start = time.time()
        print(f"\n初始化 {MAX_WORKERS} 个并行 worker...")
        pool = build_driver_pool(MAX_WORKERS, seed_cookies)

        fetch_item_detail = make_pool_fetcher(pool)
        urls_to_fetch = [item["url"] for item in to_fetch_detail]
        created_at_map = {item["url"]: item["created_at"] for item in to_fetch_detail}
        status_map = {item["url"]: item.get("status", "") for item in to_fetch_detail}

        print(f"开始并行抓取 {len(urls_to_fetch)} 个详情页（{MAX_WORKERS} workers）...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            results = list(executor.map(fetch_item_detail, urls_to_fetch))

        # Drain and quit worker drivers
        while not pool.empty():
            pool.get().quit()

        # Write results to DB sequentially (SQLite is not thread-safe for writes)
        for r in results:
            if r["error"]:
                detail_errors += 1
                print(f"  [ERROR] {r['url']}: {r['error']}")
                continue
            result = save_or_update_product(
                r["url"], r["title"], r["price"],
                status_map[r["url"]], created_at_map[r["url"]], r["raw_text"]
            )
            if result == "inserted":
                detail_inserted += 1
            else:
                detail_updated += 1
            print(f"  {'新增' if result == 'inserted' else '更新'}：{r['title']} / {r['price']}")

        phase2_elapsed = time.time() - phase2_start

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total_inserted = direct_inserted + detail_inserted
    total_updated = direct_updated + detail_updated
    total_elapsed = time.time() - sync_start

    print(f"\n{'=' * 56}")
    print(f"  同步完成")
    print(f"  共检测到：        {total_count:>4} 件")
    print(f"  新增：            {total_inserted:>4} 件")
    print(f"  更新：            {total_updated:>4} 件")
    print(f"  跳过（未変化）：  {len(to_skip):>4} 件")
    print(f"  详情页打开：      {len(to_fetch_detail):>4} 件  "
          f"（{MAX_WORKERS} 个并行 worker）")
    if detail_errors:
        print(f"  抓取失败：        {detail_errors:>4} 件")
    print(f"  阶段1耗时（列表）：{phase1_elapsed:>6.1f} 秒")
    if to_fetch_detail:
        print(f"  阶段2耗时（详情）：{phase2_elapsed:>6.1f} 秒  "
              f"（顺序预计 {phase2_elapsed * MAX_WORKERS:.1f} 秒）")
    print(f"  总耗时：           {total_elapsed:>6.1f} 秒")
    print(f"{'=' * 56}")

    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    init_db()
    webbrowser.open("http://127.0.0.1:5000")
    app.run(debug=False)
