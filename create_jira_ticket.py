"""
Jira task creation tool for the Mercari Inventory App.

Usage:
    python create_jira_ticket.py
    python create_jira_ticket.py --summary "タイトル"
    python create_jira_ticket.py --summary "タイトル" --description "説明..."

Settings are read from .env in the current directory:
    JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY
"""
import argparse
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_URL = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "")

DEFAULT_SUMMARY = "[Mercari库存App] 新規タスク"

DEFAULT_DESCRIPTION = """\
【背景/目的】
Mercari 库存管理アプリに関する新規タスクです。

【対応内容】
（具体的な作業内容を記載してください）

【完了条件】
- Flask UI での動作確認が取れていること
- 既存の同期ロジック（mercari_sync.py）への影響がないこと
- products.db のスキーマが変わっていないこと\
"""


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config() -> None:
    """Exit with a clear message if any required .env value is missing."""
    missing = [
        name for name, val in [
            ("JIRA_URL",          JIRA_URL),
            ("JIRA_EMAIL",        JIRA_EMAIL),
            ("JIRA_API_TOKEN",    JIRA_API_TOKEN),
            ("JIRA_PROJECT_KEY",  PROJECT_KEY),
        ]
        if not val
    ]
    if missing:
        print("Error: .env に以下の項目が設定されていません:")
        for key in missing:
            print(f"  {key}=<値を設定してください>")
        print("\n.env ファイルをプロジェクトルートに置き、上記キーを追加してください。")
        sys.exit(1)


# ---------------------------------------------------------------------------
# ADF builder
# ---------------------------------------------------------------------------

def text_to_adf(text: str) -> dict:
    """
    Convert plain text to Atlassian Document Format (ADF).

    Double newlines become separate paragraph nodes so the description
    renders with proper paragraph breaks in Jira.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": para}],
            }
            for para in paragraphs
        ],
    }


# ---------------------------------------------------------------------------
# Error decoding
# ---------------------------------------------------------------------------

def _jira_error_detail(body: dict) -> str:
    """Extract field-level and message-level errors from a Jira error body."""
    lines = []
    for field, msg in body.get("errors", {}).items():
        lines.append(f"  field '{field}': {msg}")
    for msg in body.get("errorMessages", []):
        lines.append(f"  {msg}")
    return "\n".join(lines)


def _handle_error(response: requests.Response) -> None:
    """Print a human-readable error and exit(1). Never prints the API token."""
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}

    detail = _jira_error_detail(body)

    if status == 401:
        print("Error: 認証エラー（401 Unauthorized）")
        print("  JIRA_EMAIL または JIRA_API_TOKEN が正しくありません。")
        print("  https://id.atlassian.com/manage-profile/security/api-tokens")
        print("  でトークンを再発行し、.env を更新してください。")

    elif status == 403:
        print("Error: 権限エラー（403 Forbidden）")
        print(f"  プロジェクト '{PROJECT_KEY}' への書き込み権限がありません。")
        print("  Jira 管理者にプロジェクトメンバーへの追加を依頼してください。")

    elif status == 404:
        print("Error: Not Found（404）")
        print(f"  JIRA_URL '{JIRA_URL}' が正しくないか、")
        print(f"  プロジェクト '{PROJECT_KEY}' が存在しません。")

    elif status == 400:
        body_str = str(body).lower()
        if "project" in body_str:
            print("Error: プロジェクトキーエラー（400 Bad Request）")
            print(f"  JIRA_PROJECT_KEY '{PROJECT_KEY}' が見つかりません。")
            print("  Jira でプロジェクトキーを確認してください。")
        elif "issuetype" in body_str:
            print("Error: イシュータイプエラー（400 Bad Request）")
            print("  'Task' がこのプロジェクトで使用できません。")
            print("  Jira のプロジェクト設定 → イシュータイプ で確認してください。")
        else:
            print(f"Error: リクエストエラー（400 Bad Request）")
        if detail:
            print(f"  詳細:\n{detail}")

    else:
        print(f"Error: 予期しないエラー（{status}）")
        if detail:
            print(f"  詳細:\n{detail}")

    sys.exit(1)


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def create_ticket(summary: str, description: str) -> str:
    """
    Create a Jira Task and return the issue key (e.g. 'KAN-12').

    Prints the issue key and browse URL on success.
    Prints a diagnostic error message and exits on failure.
    The API token is never included in any output.
    """
    validate_config()

    print(f"Jira:    {JIRA_URL}")
    print(f"Project: {PROJECT_KEY}")
    print(f"User:    {JIRA_EMAIL}")
    print()

    payload = {
        "fields": {
            "project":     {"key": PROJECT_KEY},
            "summary":     summary,
            "description": text_to_adf(description),
            "issuetype":   {"name": "Task"},
        }
    }

    try:
        response = requests.post(
            f"{JIRA_URL}/rest/api/3/issue",
            json=payload,
            headers={
                "Accept":       "application/json",
                "Content-Type": "application/json",
            },
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            timeout=15,
        )
    except requests.exceptions.ConnectionError:
        print("Error: Jira に接続できません。")
        print(f"  JIRA_URL: {JIRA_URL}")
        print("  URL が正しいか、ネットワーク接続を確認してください。")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("Error: Jira への接続がタイムアウトしました（15 秒）。")
        print("  ネットワーク状況を確認してください。")
        sys.exit(1)

    if response.status_code == 201:
        issue_key = response.json()["key"]
        issue_url = f"{JIRA_URL}/browse/{issue_key}"
        print(f"✓ Ticket 作成成功")
        print(f"  Issue Key : {issue_key}")
        print(f"  URL       : {issue_url}")
        return issue_key

    _handle_error(response)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_jira_ticket.py",
        description="Mercari Inventory App — Jira タスク作成ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
例:
  # サマリーと説明を両方指定
  python create_jira_ticket.py \\
    --summary "[Mercari库存App] 価格未取得商品の補完対応" \\
    --description "背景: 価格が空の商品が増加しています..."

  # サマリーのみ（説明はデフォルトテンプレートを使用）
  python create_jira_ticket.py \\
    --summary "[Mercari库存App] 同期速度の改善"

  # 全てデフォルト（テンプレートのままチケット作成）
  python create_jira_ticket.py

デフォルトサマリー:
  {DEFAULT_SUMMARY}
""",
    )
    parser.add_argument(
        "--summary",
        default=DEFAULT_SUMMARY,
        metavar="TEXT",
        help=f"チケットのサマリー（デフォルト: '{DEFAULT_SUMMARY}'）",
    )
    parser.add_argument(
        "--description",
        default=DEFAULT_DESCRIPTION,
        metavar="TEXT",
        help="チケットの説明（省略時はデフォルトテンプレートを使用）",
    )
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    create_ticket(args.summary, args.description)
