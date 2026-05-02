import time
import webbrowser
import sqlite3
import html

from flask import Flask, redirect
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

DB_NAME = "products.db"

app = Flask(__name__)

TIME_KEYWORDS = [
    "秒前", "分前", "時間前", "日前",
    "ヶ月前", "か月前", "年前",
    "半年前", "半年以上前"
]


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
    columns = [column[1] for column in cursor.fetchall()]

    if "created_at" not in columns:
        cursor.execute("ALTER TABLE mercari_products ADD COLUMN created_at TEXT")

    conn.commit()
    conn.close()


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
        title = html.escape(p[0] or "名称未取得")
        price = html.escape(p[1] or "-")
        url = html.escape(p[2] or "")
        created_at = html.escape(p[3] or "-")
        synced_at = html.escape(p[4] or "-")

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
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background: #f5f6f8;
                padding: 30px;
                color: #222;
            }}
            h1 {{
                font-size: 32px;
                margin-bottom: 8px;
            }}
            .top {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 20px;
            }}
            .summary {{
                color: #666;
            }}
            button {{
                background: #222;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 18px;
                font-size: 14px;
                cursor: pointer;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 12px;
                overflow: hidden;
                box-shadow: 0 4px 12px rgba(0,0,0,0.08);
            }}
            th {{
                background: #222;
                color: white;
                text-align: left;
                padding: 12px;
                font-size: 14px;
            }}
            td {{
                padding: 12px;
                border-bottom: 1px solid #eee;
                font-size: 14px;
                vertical-align: top;
            }}
            tr:hover {{
                background: #f9f9f9;
            }}
            .price {{
                font-weight: bold;
                color: #d32f2f;
                white-space: nowrap;
            }}
            a {{
                color: #0066cc;
                font-weight: 600;
                text-decoration: none;
            }}
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
                <th>No.</th>
                <th>商品名</th>
                <th>价格</th>
                <th>商品登录时间</th>
                <th>抓取时间</th>
                <th>链接</th>
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


def extract_created_at(text):
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    for line in lines:
        if "更新" in line:
            for keyword in TIME_KEYWORDS:
                if keyword in line:
                    return line

    for line in lines:
        for keyword in TIME_KEYWORDS:
            if keyword in line:
                return line

    return ""


def get_target_count(driver):
    body_text = driver.find_element(By.TAG_NAME, "body").text
    lines = [line.strip() for line in body_text.split("\n") if line.strip()]

    candidate_numbers = []

    for line in lines:
        if "商品" in line or "件" in line or "出品" in line:
            number_text = ""

            for ch in line:
                if ch.isdigit():
                    number_text += ch

            if number_text:
                candidate_numbers.append(int(number_text))

    if candidate_numbers:
        return max(candidate_numbers)

    return None


def collect_items_from_page(driver):
    links = driver.find_elements(By.TAG_NAME, "a")
    items = []
    seen_urls = set()

    for a in links:
        href = a.get_attribute("href")
        text = a.text.strip()

        if href and "/item/" in href and href not in seen_urls:
            created_at = extract_created_at(text)

            items.append({
                "url": href,
                "created_at": created_at
            })

            seen_urls.add(href)

    return items


def find_more_button(driver):
    candidates = driver.find_elements(By.TAG_NAME, "button")
    candidates += driver.find_elements(By.TAG_NAME, "a")

    for el in candidates:
        text = el.text.strip()

        if "もっと見る" in text or "もっとみる" in text:
            return el

    return None


def get_item_urls(driver):
    driver.get("https://jp.mercari.com/mypage/listings")
    print("正在进入出品列表页面...")

    time.sleep(5)

    target_count = get_target_count(driver)

    if target_count:
        print(f"目标商品数量：{target_count}")
    else:
        print("未能取得目标商品数量，将加载到没有「もっと見る」为止")

    max_clicks = 30

    for click_count in range(max_clicks):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

        current_items = collect_items_from_page(driver)
        current_count = len(current_items)

        print(f"当前已加载商品数：{current_count}")

        if target_count and current_count >= target_count:
            print("已达到目标商品数量，停止加载")
            break

        more_button = find_more_button(driver)

        if more_button:
            print(f"第 {click_count + 1} 次点击「もっと見る」按钮...")
            driver.execute_script("arguments[0].click();", more_button)
            time.sleep(4)
        else:
            print("没有找到「もっと見る」按钮，停止加载")
            break

    final_items = collect_items_from_page(driver)
    print(f"最终取得商品链接数量：{len(final_items)}")

    return final_items


def get_existing_product(item_url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT title, price, created_at
        FROM mercari_products
        WHERE item_url = ?
    """, (item_url,))

    row = cursor.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "title": row[0],
        "price": row[1],
        "created_at": row[2]
    }


def should_fetch_detail(item_url, created_at):
    existing = get_existing_product(item_url)

    if not existing:
        return True

    old_title = existing["title"]
    old_created_at = existing["created_at"]

    if not old_title:
        return True

    if old_title == "名称未取得":
        return True

    if old_title.replace(",", "").isdigit():
        return True

    if old_title in ["公開停止中", "出品停止中"]:
        return True

    if created_at and old_created_at != created_at:
        return True

    return False


def save_or_update_product(item_url, title, price, created_at, raw_text):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT id FROM mercari_products WHERE item_url = ?", (item_url,))
    existing = cursor.fetchone()

    if existing:
        cursor.execute("""
            UPDATE mercari_products
            SET title = ?,
                price = ?,
                created_at = ?,
                raw_text = ?,
                synced_at = CURRENT_TIMESTAMP
            WHERE item_url = ?
        """, (title, price, created_at, raw_text, item_url))

        conn.commit()
        conn.close()
        return "updated"

    cursor.execute("""
        INSERT INTO mercari_products
        (item_url, title, price, created_at, raw_text, synced_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (item_url, title, price, created_at, raw_text))

    conn.commit()
    conn.close()
    return "inserted"


def update_synced_time_only(item_url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE mercari_products
        SET synced_at = CURRENT_TIMESTAMP
        WHERE item_url = ?
    """, (item_url,))

    conn.commit()
    conn.close()


def click_login_button_if_exists(driver):
    time.sleep(2)

    candidates = driver.find_elements(By.TAG_NAME, "button")
    candidates += driver.find_elements(By.TAG_NAME, "a")

    for el in candidates:
        text = el.text.strip().lower()

        if "ログイン" in text or "login" in text:
            try:
                driver.execute_script("arguments[0].click();", el)
                print("ログイン按钮已自动点击")
                time.sleep(2)
                return
            except Exception:
                pass

    print("没有找到可自动点击的ログイン按钮，请手动点击。")


def scrape_item_detail(driver, url):
    driver.get(url)
    time.sleep(1)

    raw_text = driver.find_element(By.TAG_NAME, "body").text

    title = ""
    price = ""

    h1_list = driver.find_elements(By.TAG_NAME, "h1")
    if h1_list:
        title = h1_list[0].text.strip()

    body_lines = [
        line.strip()
        for line in raw_text.split("\n")
        if line.strip()
    ]

    for line in body_lines:
        if line.startswith("¥"):
            price = line
            break

    return title, price, raw_text


def run_scraper():
    options = Options()
    options.add_argument("--start-maximized")

    prefs = {
        "profile.managed_default_content_settings.images": 2
    }
    options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(options=options)

    driver.get("https://jp.mercari.com/login")

    click_login_button_if_exists(driver)

    input("请在Mercari登录完成后，回到Terminal按回车开始同步...")

    items = get_item_urls(driver)

    print(f"取得商品链接数量：{len(items)}")

    inserted_count = 0
    updated_count = 0
    skipped_count = 0

    for index, item in enumerate(items, start=1):
        url = item["url"]
        created_at = item["created_at"]

        print(f"\n{index}/{len(items)} 确认商品：{url}")

        if not should_fetch_detail(url, created_at):
            skipped_count += 1
            update_synced_time_only(url)
            print("跳过：商品未变化")
            continue

        print("需要更新，正在打开详情页...")

        title, price, raw_text = scrape_item_detail(driver, url)

        result = save_or_update_product(
            item_url=url,
            title=title,
            price=price,
            created_at=created_at,
            raw_text=raw_text
        )

        if result == "inserted":
            inserted_count += 1
            print(f"新增：{title} / {price} / {created_at}")
        else:
            updated_count += 1
            print(f"更新：{title} / {price} / {created_at}")

    print(
        f"\n同步完成：新增 {inserted_count} 个，"
        f"更新 {updated_count} 个，跳过 {skipped_count} 个。"
    )

    driver.quit()

    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    init_db()
    webbrowser.open("http://127.0.0.1:5000")
    app.run(debug=False)