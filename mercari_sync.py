import re
import time
import webbrowser
import sqlite3
import html as html_module

from flask import Flask, redirect
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

DB_NAME = "products.db"

app = Flask(__name__)

TIME_KEYWORDS = [
    "秒前", "分前", "時間前", "日前",
    "ヶ月前", "か月前", "年前",
    "半年前", "半年以上前",
]

INVALID_TITLES = {"公開停止中", "出品停止中", "売却済み", "出品中", "名称未取得"}


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
        SELECT title, price, item_url, created_at, synced_at
        FROM mercari_products
        ORDER BY id DESC
    """)
    products = cursor.fetchall()
    conn.close()

    rows = ""
    for i, p in enumerate(products, start=1):
        title = html_module.escape(p[0] or "名称未取得")
        price = html_module.escape(p[1] or "-")
        url = html_module.escape(p[2] or "")
        created_at = html_module.escape(p[3] or "-")
        synced_at = html_module.escape(p[4] or "-")
        rows += f"""
        <tr>
            <td>{i}</td>
            <td>{title}</td>
            <td class="price">{price}</td>
            <td>{created_at}</td>
            <td>{synced_at}</td>
            <td><a href="{url}" target="_blank">打开</a></td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Mercari库存管理</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                   background: #f5f6f8; padding: 30px; color: #222; }}
            h1 {{ font-size: 32px; margin-bottom: 8px; }}
            .top {{ display: flex; justify-content: space-between; align-items: center;
                    margin-bottom: 20px; }}
            .summary {{ color: #666; }}
            button {{ background: #222; color: white; border: none; border-radius: 8px;
                      padding: 10px 18px; font-size: 14px; cursor: pointer; }}
            table {{ width: 100%; border-collapse: collapse; background: white;
                     border-radius: 12px; overflow: hidden;
                     box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
            th {{ background: #222; color: white; text-align: left;
                  padding: 12px; font-size: 14px; }}
            td {{ padding: 12px; border-bottom: 1px solid #eee;
                  font-size: 14px; vertical-align: top; }}
            tr:hover {{ background: #f9f9f9; }}
            .price {{ font-weight: bold; color: #d32f2f; white-space: nowrap; }}
            a {{ color: #0066cc; font-weight: 600; text-decoration: none; }}
        </style>
    </head>
    <body>
        <h1>Mercari库存管理</h1>
        <div class="top">
            <div class="summary">商品总数：{len(products)} 件</div>
            <form method="POST" action="/sync">
                <button type="submit">同步Mercari商品</button>
            </form>
        </div>
        <table>
            <tr>
                <th>No.</th><th>商品名</th><th>价格</th>
                <th>商品登录时间</th><th>抓取时间</th><th>链接</th>
            </tr>
            {rows}
        </table>
    </body>
    </html>
    """


@app.route("/sync", methods=["POST"])
def sync():
    run_scraper()
    return redirect("/")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_listing_text(text):
    """Extract title, price, created_at from a listing-page link's visible text.

    Mercari renders each listing card as a single <a> element whose .text
    contains all visible fields separated by newlines.
    """
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    price = ""
    title = ""
    created_at = ""

    for i, line in enumerate(lines):
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

    ignore = {"¥", "公開停止中", "出品中", "売却済み", "出品停止中"}
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

    return title, price, created_at


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
        title, price, created_at = parse_listing_text(text) if text else ("", "", "")

        items.append({
            "url": href,
            "title": title,
            "price": price,
            "created_at": created_at,
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
        f"SELECT item_url, title, price, created_at FROM mercari_products "
        f"WHERE item_url IN ({placeholders})",
        urls,
    )
    result = {row[0]: {"title": row[1], "price": row[2], "created_at": row[3]}
              for row in cursor.fetchall()}
    conn.close()
    return result


def save_or_update_product(item_url, title, price, created_at, raw_text):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM mercari_products WHERE item_url = ?", (item_url,))
    exists = cursor.fetchone()

    if exists:
        cursor.execute("""
            UPDATE mercari_products
            SET title = ?, price = ?, created_at = ?, raw_text = ?,
                synced_at = CURRENT_TIMESTAMP
            WHERE item_url = ?
        """, (title, price, created_at, raw_text, item_url))
        conn.commit()
        conn.close()
        return "updated"

    cursor.execute("""
        INSERT INTO mercari_products (item_url, title, price, created_at, raw_text, synced_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (item_url, title, price, created_at, raw_text))
    conn.commit()
    conn.close()
    return "inserted"


def touch_synced_at(item_url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE mercari_products SET synced_at = CURRENT_TIMESTAMP WHERE item_url = ?",
        (item_url,),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Detail-page scraping (only used when listing-page data is insufficient)
# ---------------------------------------------------------------------------

def scrape_item_detail(driver, url):
    """Open a product detail page and extract title, price, raw_text."""
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

    return title, price, raw_text


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


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def run_scraper():
    sync_start = time.time()

    options = Options()
    options.add_argument("--start-maximized")
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2
    })

    driver = webdriver.Chrome(options=options)
    driver.get("https://jp.mercari.com/login")
    click_login_button_if_exists(driver)
    input("请在Mercari登录完成后，回到Terminal按回车开始同步...")

    # Step 1: collect all listing URLs + metadata without opening detail pages
    items = load_all_listings(driver)
    total_count = len(items)
    print(f"\n共检测到商品：{total_count} 件")

    # Step 2: batch-fetch DB state for all URLs in one query
    existing_map = fetch_existing_batch([item["url"] for item in items])

    inserted_count = 0
    updated_count = 0
    skipped_count = 0
    detail_fetches = 0

    for idx, item in enumerate(items, start=1):
        url = item["url"]
        listing_title = item["title"]
        listing_price = item["price"]
        listing_created_at = item["created_at"]
        listing_raw = item["raw_text"]

        existing = existing_map.get(url)

        # --- Skip: item unchanged (valid title in DB, created_at matches) ---
        if existing:
            old_title = existing["title"] or ""
            old_created_at = existing["created_at"] or ""
            if is_valid_title(old_title) and old_created_at == listing_created_at:
                skipped_count += 1
                touch_synced_at(url)
                print(f"[{idx}/{total_count}] 跳过（未变化）：{old_title}")
                continue

        # --- Use listing-page data directly when title + price are available ---
        if is_valid_title(listing_title) and listing_price:
            result = save_or_update_product(
                url, listing_title, listing_price, listing_created_at, listing_raw
            )
            print(f"[{idx}/{total_count}] {'新增' if result == 'inserted' else '更新'}（列表页）："
                  f"{listing_title} / {listing_price}")
        else:
            # Fall back to detail page when listing-page data is incomplete
            detail_fetches += 1
            print(f"[{idx}/{total_count}] 打开详情页：{url}")
            title, price, raw_text = scrape_item_detail(driver, url)
            result = save_or_update_product(
                url, title, price, listing_created_at, raw_text
            )
            print(f"  → {'新增' if result == 'inserted' else '更新'}：{title} / {price}")

        if result == "inserted":
            inserted_count += 1
        else:
            updated_count += 1

    driver.quit()

    elapsed = time.time() - sync_start
    print(f"\n{'=' * 52}")
    print(f"  同步完成")
    print(f"  共检测到：{total_count:>4} 件")
    print(f"  新增：    {inserted_count:>4} 件")
    print(f"  更新：    {updated_count:>4} 件")
    print(f"  跳过：    {skipped_count:>4} 件（未变化）")
    print(f"  详情页：  {detail_fetches:>4} 次（数据不足时打开）")
    print(f"  总耗时：  {elapsed:.1f} 秒")
    print(f"{'=' * 52}")

    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    init_db()
    webbrowser.open("http://127.0.0.1:5000")
    app.run(debug=False)
