from flask import Flask
import sqlite3

app = Flask(__name__)

DB_NAME = "products.db"


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

    html = """
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Mercari库存管理</title>
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                background: #f5f6f8;
                padding: 30px;
                color: #222;
            }

            h1 {
                margin-bottom: 8px;
                font-size: 32px;
            }

            .summary {
                margin-bottom: 20px;
                color: #666;
                font-size: 16px;
            }

            table {
                width: 100%;
                border-collapse: collapse;
                background: white;
                border-radius: 12px;
                overflow: hidden;
                box-shadow: 0 4px 12px rgba(0,0,0,0.08);
            }

            th {
                background: #222;
                color: white;
                text-align: left;
                padding: 12px;
                font-size: 14px;
            }

            td {
                padding: 12px;
                border-bottom: 1px solid #eee;
                vertical-align: top;
                font-size: 14px;
            }

            tr:hover {
                background: #f9f9f9;
            }

            a {
                color: #0066cc;
                text-decoration: none;
                font-weight: 600;
            }

            a:hover {
                text-decoration: underline;
            }

            .price {
                font-weight: bold;
                color: #d32f2f;
                white-space: nowrap;
            }

            .no-title {
                color: #999;
            }

            .date {
                white-space: nowrap;
                color: #555;
            }

            .url {
                white-space: nowrap;
            }
        </style>
    </head>
    <body>
        <h1>Mercari库存管理</h1>
        <div class="summary">商品总数：{} 件</div>

        <table>
            <tr>
                <th>No.</th>
                <th>商品名</th>
                <th>价格</th>
                <th>商品登录时间</th>
                <th>抓取时间</th>
                <th>商品链接</th>
            </tr>
    """.format(len(products))

    for i, p in enumerate(products, start=1):
        title = p[0] or "名称未取得"
        price = p[1] or "-"
        url = p[2]
        created_at = p[3] or "-"
        synced_at = p[4] or "-"

        title_class = "no-title" if title == "名称未取得" else ""

        html += f"""
            <tr>
                <td>{i}</td>
                <td class="{title_class}">{title}</td>
                <td class="price">{price}</td>
                <td class="date">{created_at}</td>
                <td class="date">{synced_at}</td>
                <td class="url">
                    <a href="{url}" target="_blank">打开商品页面</a>
                </td>
            </tr>
        """

    html += """
        </table>
    </body>
    </html>
    """

    return html


if __name__ == "__main__":
    app.run(debug=True)