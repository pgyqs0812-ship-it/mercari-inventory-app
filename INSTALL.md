# MIA Inventory — インストールガイド

## 重要事項 / Important Notice

このソフトウェアは独立した個人開発のツールです。
メルカリ株式会社とは一切関係がなく、同社による公認・後援・推奨を受けていません。

> This is an independent third-party tool.
> It is **not** affiliated with, sponsored by, or endorsed by Mercari Inc.

---

## システム要件 / System Requirements

| 項目 | 要件 |
|---|---|
| OS | macOS 12 Monterey 以降 |
| ブラウザ | Google Chrome（最新版推奨） |
| ChromeDriver | 不要（Selenium Manager が自動管理） |

---

## インストール手順 / Installation

1. **DMG を開く** — `MIAInventory_Mac_vX.X.X.dmg` をダブルクリック
2. **インストーラウィンドウが表示されたら** — アプリアイコンを右の Applications フォルダへドラッグ
3. **DMG を取り出す** — サイドバーの取り出しボタンをクリック（またはゴミ箱へドラッグ）
4. **アプリを起動する** — Finder の「アプリケーション」フォルダから `MercariInventory` を開く

```
[ MercariInventory.app ]  →→→  [ Applications ]
```

---

## 初回起動時の Gatekeeper 対処 / First-Launch Gatekeeper Bypass

未署名ビルド（Developer ID 未設定）の場合、macOS が「開発元を確認できません」と表示することがあります。

**解決方法：**

1. Finder でアプリを **右クリック（または Control+クリック）**
2. メニューから **「開く」** を選択
3. 確認ダイアログで **「開く」** をクリック

この操作は初回のみ必要です。以降は通常通りダブルクリックで起動できます。

> **For unsigned builds:** Right-click → Open → Open (in dialog).
> This is only needed once.

---

## アプリの使い方 / Usage

1. アプリを起動するとブラウザが自動的に開きます
2. 「ログイン」ボタンをクリックし、メルカリアカウントでログイン
3. 「同期」ボタンで出品中・売り切れ商品の在庫情報を取得
4. 商品一覧の検索・フィルタ・CSV/Excel エクスポートが利用できます

---

## データの保存場所 / Data Location

```
~/Library/Application Support/MercariInventory/
  products.db          ← 商品データベース
  license.json         ← ライセンス情報
  mercari_session.json ← ログインセッション
  app.log              ← 起動ログ（トラブルシューティング用）
```

アプリを削除しても上記フォルダのデータは残ります。
完全削除する場合はこのフォルダも手動で削除してください。

---

## アンインストール / Uninstall

1. `/Applications/MercariInventory.app` をゴミ箱へ移動
2. データも削除する場合: `~/Library/Application Support/MercariInventory/` を削除

---

## トラブルシューティング / Troubleshooting

| 症状 | 対処 |
|---|---|
| Chrome が見つからないというエラー | [Google Chrome](https://www.google.com/chrome/) をインストール |
| ポート 5050 が使用中 | すでに起動中の場合はブラウザで `http://127.0.0.1:5050` を開く |
| 起動しない / クラッシュする | `~/Library/Application Support/MercariInventory/app.log` を確認 |
| Gatekeeper ブロック | 上記「初回起動時の Gatekeeper 対処」を参照 |

---

*MIA Inventory — Mercari 販売者向け独立型在庫管理ツール*
