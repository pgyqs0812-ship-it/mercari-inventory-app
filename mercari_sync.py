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

import io
import csv

from flask import Flask, redirect, request, Response
import openpyxl
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

DB_NAME = "products.db"
MAX_WORKERS = 4
MAX_RETRY = 3

_JST = timezone(timedelta(hours=9))

# Statuses with larger listing counts that need a longer pagination timeout
_LONG_TIMEOUT_STATUSES = {"売却済み", "販売履歴"}

# Populated at the end of each sync run; read by home() to show the popup
_last_sync_summary: dict = {}


def jst_now() -> str:
    """Return the current time in JST as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.now(tz=_JST).strftime("%Y-%m-%d %H:%M:%S")

app = Flask(__name__)

TIME_KEYWORDS = [
    "秒前", "分前", "時間前", "日前",
    "ヶ月前", "か月前", "年前",
    "半年前", "半年以上前",
]

INVALID_TITLES = {"公開停止中", "出品停止中", "売却済み", "出品中", "取引中", "名称未取得", "販売履歴"}

STATUSES = ["出品中", "取引中", "売却済み", "販売履歴"]

# Mercari mypage URL for each status
STATUS_URLS = {
    "出品中":   "https://jp.mercari.com/mypage/listings",
    "取引中":   "https://jp.mercari.com/mypage/listings/in_progress",
    "売却済み": "https://jp.mercari.com/mypage/listings/completed",
    "販売履歴": "https://jp.mercari.com/mypage/listings/sold",
}


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
    # Backfill: records saved before per-status scraping have status=''.
    # Treat them as 出品中 so IN-based search works correctly.
    cursor.execute(
        "UPDATE mercari_products SET status = '出品中' WHERE status IS NULL OR status = ''"
    )
    # KAN-10: correct status names to match updated URL mapping.
    # ORDER MATTERS: rename 売却済み→販売履歴 first so the second rename
    # (公開停止中→売却済み) does not collide with records just renamed.
    cursor.execute(
        "UPDATE mercari_products SET status = '販売履歴' WHERE status = '売却済み'"
    )
    cursor.execute(
        "UPDATE mercari_products SET status = '売却済み' WHERE status = '公開停止中'"
    )
    conn.commit()
    conn.close()


# Badge style per status: (bg_color, text_color)
STATUS_BADGE = {
    "出品中":    ("#dcfce7", "#166534"),
    "取引中":    ("#fef9c3", "#854d0e"),
    "売却済み":  ("#f3f4f6", "#374151"),
    "公開停止中": ("#fee2e2", "#991b1b"),  # legacy — kept for display of old records
    "販売履歴":  ("#dbeafe", "#1e40af"),
}


def _query_products(q="", statuses=None):
    """Query mercari_products filtered by keyword and status list."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    sql = ("SELECT title, price, item_url, created_at, synced_at, status "
           "FROM mercari_products WHERE 1=1")
    params = []
    if q:
        sql += " AND title LIKE ?"
        params.append(f"%{q}%")
    if statuses:
        ph = ",".join("?" * len(statuses))
        sql += f" AND status IN ({ph})"
        params.extend(statuses)
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
        box-shadow: var(--shadow-md); overflow: hidden; }
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
table { width: 100%; border-collapse: collapse; }
thead th { background: #f9fafb; color: var(--muted); font-size: 12px;
           font-weight: 600; text-transform: uppercase; letter-spacing: .05em;
           padding: 10px 14px; text-align: left;
           border-bottom: 2px solid var(--border); }
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
"""


def _badge_html(status):
    bg, fg = STATUS_BADGE.get(status, ("#f3f4f6", "#374151"))
    s = html_module.escape(status or "—")
    return f'<span class="badge" style="background:{bg};color:{fg}">{s}</span>'


def _build_result_rows(products):
    rows = ""
    for i, p in enumerate(products, start=1):
        title     = html_module.escape(p[0] or "名称未取得")
        price     = html_module.escape(p[1] or "—")
        url       = html_module.escape(p[2] or "")
        created   = html_module.escape(p[3] or "—")
        synced    = html_module.escape(p[4] or "—")
        badge     = _badge_html(p[5] or "")
        rows += f"""
        <tr>
          <td style="color:var(--muted);font-size:12px">{i}</td>
          <td>{title}</td>
          <td class="price">{price}</td>
          <td>{badge}</td>
          <td style="color:var(--muted)">{created}</td>
          <td style="color:var(--muted)">{synced}</td>
          <td><a class="link-btn" href="{url}" target="_blank">開く ↗</a></td>
        </tr>"""
    return rows


@app.route("/")
def home():
    searched      = request.args.get("searched") == "1"
    q             = request.args.get("q", "").strip()
    sel_statuses  = request.args.getlist("statuses") or list(STATUSES)
    show_summary  = request.args.get("summary") == "1" and bool(_last_sync_summary)

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM mercari_products")
    total_db = cursor.fetchone()[0]
    conn.close()

    products = _query_products(q, sel_statuses) if searched else []
    count    = len(products)

    if searched:
        per_status = {}
        for p in products:
            s = p[5] or "出品中"
            per_status[s] = per_status.get(s, 0) + 1
        status_summary = ", ".join(f"{s}={c}" for s, c in per_status.items())
        print(f"[search] selected: {', '.join(sel_statuses)}")
        print(f"[search] result: {status_summary or '0件'} (total={count})")

    # ── sync card checkboxes ──────────────────────────────────────────────
    sync_cbs = ""
    for s in STATUSES:
        sync_cbs += (f'<label class="cb-label">'
                     f'<input type="checkbox" name="statuses" value="{s}" checked> {s}'
                     f'</label>\n')

    # ── search filter checkboxes (reflect current selection) ─────────────
    search_cbs = ""
    for s in STATUSES:
        chk = "checked" if s in sel_statuses else ""
        search_cbs += (f'<label class="cb-label">'
                       f'<input type="checkbox" name="statuses" value="{s}" {chk}> {s}'
                       f'</label>\n')

    # ── results section ───────────────────────────────────────────────────
    if not searched:
        results_html = ""
    else:
        rows_html = _build_result_rows(products)
        disabled  = 'disabled' if count == 0 else ''
        empty_row = ("""<tr><td colspan="7">
            <div class="empty-state">
              <div class="es-icon">🔍</div>
              <p>該当する商品が見つかりませんでした</p>
            </div></td></tr>""" if count == 0 else "")

        results_html = f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">
              検索結果
              <span class="count-badge">{count} 件</span>
            </span>
            <div class="export-row">
              <a class="btn btn-outline" id="export-csv" href="#" {disabled}>
                ⬇ CSV
              </a>
              <a class="btn btn-outline" id="export-xlsx" href="#" {disabled}>
                ⬇ Excel
              </a>
            </div>
          </div>
          <div class="card-body" style="padding:0">
            <table>
              <thead>
                <tr>
                  <th style="width:40px">#</th>
                  <th>商品名</th>
                  <th>価格</th>
                  <th>状態</th>
                  <th>商品登録時間</th>
                  <th>抓取時間</th>
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
            xlsx_el.href = '/export/xlsx?' + sp.toString();
    """ if searched else ""

    # ── sync summary modal ────────────────────────────────────────────────
    if show_summary:
        s = _last_sync_summary
        status_rows = ""
        for st, cnt in s.get("per_status", {}).items():
            status_rows += f"<tr><td>{html_module.escape(st)}</td><td>{cnt} 件</td></tr>"
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
              <tr><td>跳過</td><td>{s.get('skipped', 0)} 件</td></tr>
              {status_rows}
            </table>
            <button class="modal-close" onclick="document.getElementById('summary-modal').remove()">閉じる</button>
          </div>
        </div>"""
    else:
        summary_modal = ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Mercari 在庫管理</title>
  <style>{_CSS}</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div>
      <h1>Mercari 在庫管理</h1>
      <p>商品データの同期・検索・エクスポート</p>
    </div>
    <div style="display:flex;gap:10px;align-items:center">
      <span class="db-pill">DB: {total_db} 件</span>
      <button class="btn-exit"
              onclick="if(confirm('アプリを終了しますか？')){{fetch('/shutdown',{{method:'POST'}}).then(()=>{{document.body.innerHTML='<p style=\\'padding:40px;font-family:sans-serif\\'>アプリを終了しました。このウィンドウを閉じてください。</p>'}})}}}}">[終了]</button>
    </div>
  </div>
</header>
<main>

  <!-- Sync card -->
  <div class="card">
    <div class="card-header">
      <span class="card-title">同期設定</span>
    </div>
    <div class="card-body">
      <form method="POST" action="/sync">
        <p class="field-label">同期するステータス</p>
        <div class="cb-row">{sync_cbs}</div>
        <button class="btn btn-primary" type="submit">&#x21BB; 同期を開始</button>
      </form>
    </div>
  </div>

  <!-- Search card -->
  <div class="card">
    <div class="card-header">
      <span class="card-title">商品を検索</span>
    </div>
    <div class="card-body">
      <form method="GET" action="/">
        <input type="hidden" name="searched" value="1">
        <div class="search-row">
          <input class="text-input" type="text" name="q"
                 placeholder="商品名で検索…" value="{html_module.escape(q)}">
          <button class="btn btn-primary" type="submit">検索</button>
        </div>
        <p class="field-label">ステータスで絞り込み</p>
        <div class="cb-row">{search_cbs}</div>
      </form>
    </div>
  </div>

  {results_html}

</main>
{summary_modal}
<script>{export_js}</script>
</body>
</html>"""


@app.route("/sync", methods=["POST"])
def sync():
    selected = request.form.getlist("statuses")
    if not selected:
        selected = list(STATUSES)
    run_scraper(selected_statuses=selected)
    return redirect("/?summary=1")


@app.route("/shutdown", methods=["POST"])
def shutdown():
    threading.Thread(
        target=lambda: (time.sleep(0.4), os._exit(0)),
        daemon=True,
    ).start()
    return Response("shutting down", status=200)


@app.route("/export/csv")
def export_csv():
    q            = request.args.get("q", "").strip()
    sel_statuses = request.args.getlist("statuses") or list(STATUSES)
    products     = _query_products(q, sel_statuses)

    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM — ensures Excel opens without garbled text
    writer = csv.writer(buf)
    writer.writerow(["状態", "商品名", "価格", "商品登録時間", "抓取時間", "リンク"])
    for p in products:
        writer.writerow([p[5] or "", p[0] or "", p[1] or "",
                         p[3] or "", p[4] or "", p[2] or ""])

    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": "attachment; filename=mercari_export.csv"},
    )


@app.route("/export/xlsx")
def export_xlsx():
    q            = request.args.get("q", "").strip()
    sel_statuses = request.args.getlist("statuses") or list(STATUSES)
    products     = _query_products(q, sel_statuses)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Mercari商品"
    ws.append(["状態", "商品名", "価格", "商品登録時間", "抓取時間", "リンク"])
    for p in products:
        ws.append([p[5] or "", p[0] or "", p[1] or "",
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

    ignore = {"¥", "公開停止中", "出品中", "取引中", "売却済み", "出品停止中", "販売履歴"}
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


def load_listings_for_status(driver, status_label, pagination_timeout=6):
    """Load all items from the Mercari page for one status, paginating fully.

    All returned items have their 'status' field set to status_label,
    overriding any badge text detected by parse_listing_text.  This is
    necessary because the 出品中 page does not show a status badge on cards,
    so parse_listing_text would return status='' for every item there.
    """
    url = STATUS_URLS.get(status_label)
    if not url:
        print(f"[{status_label}] 未知ステータス — スキップ")
        return []

    print(f"\n[{status_label}] {url} に遷移中...")
    driver.get(url)
    wait_for_items(driver, timeout=10)

    initial = collect_items_from_page(driver)
    if not initial:
        print(f"[{status_label}] 商品が見つかりませんでした")
        return []

    for click_num in range(200):
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.8)

        current_items = collect_items_from_page(driver)
        print(f"  [{status_label}] 読み込み済み: {len(current_items)} 件", end="\r", flush=True)

        more_btn = find_more_button(driver)
        if not more_btn:
            print(f"\n[{status_label}] 全件読み込み完了")
            break

        prev_count = len(current_items)
        driver.execute_script("arguments[0].click();", more_btn)
        print(f"\n[{status_label}] 「もっと見る」クリック {click_num + 1} 回目...", flush=True)
        wait_for_count_increase(driver, prev_count, timeout=pagination_timeout)

    final = collect_items_from_page(driver)
    # Force the status to the URL-based label — card text may not have a badge
    for item in final:
        item["status"] = status_label

    print(f"[{status_label}] 取得完了: {len(final)} 件")
    return final


def load_all_listings(driver, selected_statuses):
    """Load items for every selected status page, deduplicating by URL."""
    all_items = []
    seen_urls = set()
    counts = {}

    for status in selected_statuses:
        timeout = 10 if status in _LONG_TIMEOUT_STATUSES else 6
        items = load_listings_for_status(driver, status, pagination_timeout=timeout)
        new_items = [i for i in items if i["url"] not in seen_urls]
        seen_urls.update(i["url"] for i in new_items)
        all_items.extend(new_items)
        counts[status] = len(new_items)

    print("\n--- ステータス別取得件数 ---")
    for s in selected_statuses:
        print(f"  {s}: {counts.get(s, 0)}")
    print(f"  合計: {len(all_items)} 件")
    return all_items, counts


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
    global _last_sync_summary

    if selected_statuses is None:
        selected_statuses = list(STATUSES)

    start_jst = jst_now()
    sync_start = time.time()

    # ------------------------------------------------------------------
    # Phase 1: login with the main (visible) driver, collect all listings
    # ------------------------------------------------------------------
    main_driver = _make_chrome_driver(headless=False)
    main_driver.get("https://jp.mercari.com/login")
    click_login_button_if_exists(main_driver)
    wait_for_login(main_driver)

    phase1_start = time.time()
    items, per_status_counts = load_all_listings(main_driver, selected_statuses)
    total_count = len(items)

    existing_map = fetch_existing_batch([item["url"] for item in items])
    to_skip, to_save_direct, to_fetch_detail = classify_items(items, existing_map)

    print(f"  跳过（未変化）：{len(to_skip)} 件 | "
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
            print(f"  {'新増' if result == 'inserted' else '更新'}：{r['title']} / {r['price']}")

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

    _last_sync_summary = {
        "start_jst": start_jst,
        "end_jst": jst_now(),
        "elapsed": round(total_elapsed, 1),
        "per_status": per_status_counts,
        "inserted": total_inserted,
        "updated": total_updated,
        "skipped": len(to_skip),
        "total": total_count,
    }

    webbrowser.open("http://127.0.0.1:5050")


if __name__ == "__main__":
    init_db()
    webbrowser.open("http://127.0.0.1:5050")
    app.run(debug=False)
