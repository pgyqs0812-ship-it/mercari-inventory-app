from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import sqlite3
import time
import webbrowser

DB_NAME = "products.db"


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

    # 旧数据库没有 created_at 时，自动追加字段
    cursor.execute("PRAGMA table_info(mercari_products)")
    columns = [column[1] for column in cursor.fetchall()]

    if "created_at" not in columns:
        cursor.execute("""
            ALTER TABLE mercari_products
            ADD COLUMN created_at TEXT
        """)

    conn.commit()
    conn.close()


def parse_product_text(text):
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    price = ""
    title = ""
    created_at = ""

    for i, line in enumerate(lines):
        if line == "¥" and i + 1 < len(lines):
            price = "¥" + lines[i + 1]
        elif line.startswith("¥"):
            price = line

        if "前に更新" in line:
            created_at = line

    ignore_words = [
        "¥",
        "公開停止中",
        "出品中",
        "売却済み"
    ]

    for line in reversed(lines):
        if line in ignore_words:
            continue
        if line.replace(",", "").isdigit():
            continue
        if "前に更新" in line:
            continue
        if line.startswith("¥"):
            continue

        title = line
        break

    return title, price, created_at


def save_product(item_url, title, price, created_at, raw_text):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id FROM mercari_products
        WHERE item_url = ?
    """, (item_url,))

    existing = cursor.fetchone()

    if existing:
        conn.close()
        return False

    cursor.execute("""
        INSERT INTO mercari_products
        (item_url, title, price, created_at, raw_text, synced_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        item_url,
        title,
        price,
        created_at,
        raw_text
    ))

    conn.commit()
    conn.close()

    return True


init_db()

options = Options()
options.add_argument("--start-maximized")

driver = webdriver.Chrome(options=options)

# 直接打开 Mercari 登录页面
driver.get("https://jp.mercari.com/login")

input("请在Mercari登录页面完成登录后，按回车进入出品列表...")

driver.get("https://jp.mercari.com/mypage/listings")
print("正在进入出品列表页面...")

time.sleep(8)

# 滚动加载商品
driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
time.sleep(3)

links = driver.find_elements(By.TAG_NAME, "a")

saved_count = 0
skipped_count = 0

for a in links:
    href = a.get_attribute("href")
    text = a.text.strip()

    if href and "/item/" in href and text:
        title, price, created_at = parse_product_text(text)

        saved = save_product(
            item_url=href,
            title=title,
            price=price,
            created_at=created_at,
            raw_text=text
        )

        if saved:
            saved_count += 1
            print(f"{saved_count}. 新增保存：{title} / {price} / {created_at}")
        else:
            skipped_count += 1
            print(f"跳过已存在：{title} / {price} / {created_at}")

        print(href)

print(f"\n同步完成：新增 {saved_count} 个，跳过 {skipped_count} 个。")

driver.quit()

# 抓取完成后自动打开库存一览
webbrowser.open("http://127.0.0.1:5000")