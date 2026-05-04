"""
Jira issue creation tool for the Mercari Inventory App.

Creates a タスク in the KAN project and prints structured logs so CI runs
show exactly what was attempted and what happened.

Usage (local):
    python create_jira_ticket.py --version v1.4.3 --summary "KAN-30 — ..."

Usage (CI — credentials via environment, not .env):
    JIRA_URL=... JIRA_EMAIL=... JIRA_API_TOKEN=... JIRA_PROJECT_KEY=... \\
    python create_jira_ticket.py --version "$TAG" --summary "$MSG"

Settings are read from environment variables (CI secrets take precedence over .env):
    JIRA_URL            https://yoursite.atlassian.net
    JIRA_EMAIL          your@email.com
    JIRA_API_TOKEN      Atlassian API token
    JIRA_PROJECT_KEY    KAN

Exit codes:
    0  — issue created successfully
    1  — any failure (missing config, auth error, API error, etc.)
         When run from build.yml the non-zero exit blocks the release upload.
"""
import argparse
import os
import sys

import requests
from dotenv import load_dotenv

# .env is loaded only if the variable is not already set in the environment
# (CI secrets injected via env: block take precedence automatically).
load_dotenv()

JIRA_URL       = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
PROJECT_KEY    = os.getenv("JIRA_PROJECT_KEY", "")

# Issue type name must match the project's actual type.
# This project uses "タスク" (next-gen team-managed).  "Task" (English) is NOT
# valid here and would cause a 400 Bad Request.
ISSUE_TYPE = "タスク"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config() -> None:
    """Exit 1 with a clear message if any required env value is missing."""
    missing = [
        name for name, val in [
            ("JIRA_URL",         JIRA_URL),
            ("JIRA_EMAIL",       JIRA_EMAIL),
            ("JIRA_API_TOKEN",   JIRA_API_TOKEN),
            ("JIRA_PROJECT_KEY", PROJECT_KEY),
        ]
        if not val
    ]
    if missing:
        _log("ERROR", "必要な環境変数が設定されていません:")
        for key in missing:
            _log("ERROR", f"  {key}=<値を設定してください>")
        _log("ERROR", ".env またはリポジトリの Secrets に上記キーを追加してください。")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(level: str, msg: str) -> None:
    """Print a timestamped log line to stdout so CI captures it."""
    print(f"[jira-create] [{level}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# ADF builder
# ---------------------------------------------------------------------------

def text_to_adf(text: str) -> dict:
    """Convert plain text to Atlassian Document Format (ADF)."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text or "(no description)"]
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
    lines = []
    for field, msg in body.get("errors", {}).items():
        lines.append(f"  field '{field}': {msg}")
    for msg in body.get("errorMessages", []):
        lines.append(f"  {msg}")
    return "\n".join(lines)


def _handle_error(response: requests.Response) -> None:
    """Log a human-readable error and exit(1). Never logs the API token."""
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}

    detail = _jira_error_detail(body)

    _log("ERROR", f"Jira API エラー: HTTP {status}")

    if status == 401:
        _log("ERROR", "認証エラー (401 Unauthorized)")
        _log("ERROR", "  JIRA_EMAIL または JIRA_API_TOKEN が正しくありません。")
        _log("ERROR", "  https://id.atlassian.com/manage-profile/security/api-tokens でトークンを確認してください。")
    elif status == 403:
        _log("ERROR", f"権限エラー (403 Forbidden) — プロジェクト '{PROJECT_KEY}' への書き込み権限がありません。")
    elif status == 404:
        _log("ERROR", f"Not Found (404) — JIRA_URL '{JIRA_URL}' またはプロジェクト '{PROJECT_KEY}' が存在しません。")
    elif status == 400:
        body_str = str(body).lower()
        if "project" in body_str:
            _log("ERROR", f"プロジェクトキーエラー (400) — '{PROJECT_KEY}' が見つかりません。")
        elif "issuetype" in body_str:
            _log("ERROR", f"イシュータイプエラー (400) — '{ISSUE_TYPE}' がこのプロジェクトで使用できません。")
            _log("ERROR", "  Jira プロジェクト設定 → イシュータイプ で利用可能なタイプを確認してください。")
        else:
            _log("ERROR", f"リクエストエラー (400)")
    else:
        _log("ERROR", f"予期しないエラー ({status})")

    if detail:
        _log("ERROR", f"詳細:\n{detail}")

    sys.exit(1)


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def create_ticket(summary: str, description: str, version: str = "") -> str:
    """Create a Jira タスク and return the issue key (e.g. 'KAN-28').

    Logs structured output so CI shows exactly what was attempted.
    Calls sys.exit(1) on any failure so the calling workflow step fails
    and blocks the subsequent GitHub Release upload.
    """
    validate_config()

    _log("INFO", "=" * 60)
    _log("INFO", f"対象バージョン : {version or '(未指定)'}")
    _log("INFO", f"Jira URL       : {JIRA_URL}")
    _log("INFO", f"プロジェクト   : {PROJECT_KEY}")
    _log("INFO", f"ユーザー       : {JIRA_EMAIL}")
    _log("INFO", f"イシュータイプ : {ISSUE_TYPE}")
    _log("INFO", f"サマリー       : {summary}")
    _log("INFO", "=" * 60)

    payload = {
        "fields": {
            "project":     {"key": PROJECT_KEY},
            "summary":     summary,
            "description": text_to_adf(description),
            "issuetype":   {"name": ISSUE_TYPE},
        }
    }

    _log("INFO", f"POST {JIRA_URL}/rest/api/3/issue ...")

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
        _log("ERROR", f"Jira に接続できません: {JIRA_URL}")
        _log("ERROR", "ネットワーク接続または JIRA_URL を確認してください。")
        sys.exit(1)
    except requests.exceptions.Timeout:
        _log("ERROR", "Jira への接続がタイムアウトしました (15 秒)。")
        sys.exit(1)

    if response.status_code == 201:
        issue_key = response.json()["key"]
        issue_url = f"{JIRA_URL}/browse/{issue_key}"
        _log("INFO", "✓ Jira イシュー作成成功")
        _log("INFO", f"  Issue Key : {issue_key}")
        _log("INFO", f"  URL       : {issue_url}")
        _log("INFO", f"  Version   : {version or '(未指定)'}")
        return issue_key

    _handle_error(response)


# ---------------------------------------------------------------------------
# Transition helper (used after release succeeds)
# ---------------------------------------------------------------------------

def transition_to_done(issue_key: str, done_transition_id: str = "51") -> None:
    """Move issue_key to 完了 (Done). Logs result; does not exit on failure."""
    validate_config()
    url = f"{JIRA_URL}/rest/api/2/issue/{issue_key}/transitions"
    try:
        r = requests.post(
            url,
            json={"transition": {"id": done_transition_id}},
            headers={"Content-Type": "application/json"},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            timeout=15,
        )
        if r.status_code == 204:
            _log("INFO", f"✓ {issue_key} を 完了 に移行しました")
        else:
            _log("WARN", f"{issue_key} の移行失敗: HTTP {r.status_code} {r.text[:120]}")
    except Exception as exc:
        _log("WARN", f"{issue_key} の移行中に例外: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_jira_ticket.py",
        description="Mercari Inventory App — Jira イシュー作成ツール",
    )
    parser.add_argument(
        "--summary",
        default="[MIA] 新規リリースタスク",
        metavar="TEXT",
        help="イシューのサマリー",
    )
    parser.add_argument(
        "--description",
        default="",
        metavar="TEXT",
        help="イシューの説明（省略時は空）",
    )
    parser.add_argument(
        "--version",
        default="",
        metavar="VERSION",
        help="対象リリースバージョン (例: v1.4.3) — ログに記録されます",
    )
    parser.add_argument(
        "--done",
        metavar="ISSUE_KEY",
        help="指定したイシューキーを 完了 に移行して終了 (例: KAN-28)",
    )
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if args.done:
        transition_to_done(args.done)
    else:
        create_ticket(args.summary, args.description, args.version)
