"""
Jira issue creation and lifecycle tool for the Mercari Inventory App.

Creates a タスク in the KAN project, auto-generates a rich description from
git log, outputs the issue key to $GITHUB_OUTPUT, and closes the issue with
a release comment after the GitHub Release succeeds.

Usage (create — CI):
    python create_jira_ticket.py \
      --version v1.4.3 \
      --summary "v1.4.3 — feat: ..." \
      --release-url "https://github.com/.../releases/tag/v1.4.3"

Usage (close after release — CI):
    python create_jira_ticket.py \
      --done KAN-31 \
      --version v1.4.3 \
      --release-url "https://github.com/.../releases/tag/v1.4.3"

Usage (backfill description on existing issue):
    python create_jira_ticket.py \
      --update-description KAN-31 \
      --version v1.4.3 \
      --release-url "https://github.com/.../releases/tag/v1.4.3"

Settings (CI secrets override .env):
    JIRA_URL            https://yoursite.atlassian.net
    JIRA_EMAIL          your@email.com
    JIRA_API_TOKEN      Atlassian API token
    JIRA_PROJECT_KEY    KAN

Exit codes:
    0  success
    1  any failure — non-zero exit blocks the release upload in build.yml
"""
import argparse
import os
import subprocess
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

JIRA_URL       = os.getenv("JIRA_URL", "").rstrip("/")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")
PROJECT_KEY    = os.getenv("JIRA_PROJECT_KEY", "")

# Must match the project's actual issue type (next-gen team-managed, Japanese).
ISSUE_TYPE = "タスク"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(level: str, msg: str) -> None:
    print(f"[jira] [{level}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def validate_config() -> None:
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
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: list) -> str:
    """Run a git command; return stdout stripped, or '' on failure."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# ADF (Atlassian Document Format) node builders
# ---------------------------------------------------------------------------

def _adf_heading(level: int, text: str) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _adf_paragraph(text: str) -> dict:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text}],
    }


def _adf_code_block(text: str) -> dict:
    return {
        "type": "codeBlock",
        "attrs": {"language": "text"},
        "content": [{"type": "text", "text": text}],
    }


def text_to_adf(text: str) -> dict:
    """Convert plain text (double-newline paragraphs) to ADF."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text or "(no description)"]
    return {
        "type": "doc",
        "version": 1,
        "content": [_adf_paragraph(p) for p in paragraphs],
    }


def _build_release_adf(version: str, summary: str, release_url: str) -> dict:
    """Auto-generate an ADF description from git log + release metadata."""
    content = []

    if version:
        content.append(_adf_heading(2, f"リリースバージョン: {version}"))

    # Full commit body (may be multi-paragraph)
    commit_body = _git(["git", "log", "-1", "--pretty=%B"])
    if commit_body:
        content.append(_adf_heading(3, "変更内容"))
        for para in commit_body.split("\n\n"):
            para = para.strip()
            if para:
                content.append(_adf_paragraph(para))

    # Recent commit history
    recent = _git(["git", "log", "-5", "--oneline"])
    if recent:
        content.append(_adf_heading(3, "最近のコミット"))
        content.append(_adf_code_block(recent))

    # GitHub Release link
    if release_url:
        content.append(_adf_heading(3, "GitHub Release"))
        content.append(_adf_paragraph(release_url))

    if not content:
        content.append(_adf_paragraph(summary or "(no description)"))

    return {"type": "doc", "version": 1, "content": content}


def _build_close_comment_adf(
    version: str,
    release_url: str,
    artifacts: list = None,
) -> dict:
    """ADF comment body confirming a successful release."""
    lines = []
    if version:
        lines.append(f"バージョン       : {version}")
    if release_url:
        lines.append(f"GitHub Release   : {release_url}")
    lines.append("CI ステータス    : success ✓")
    lines.append("")
    lines.append("== リリースアーティファクト ==")
    if artifacts:
        for name in artifacts:
            lines.append(f"  {name} ✓")
    return {
        "type": "doc",
        "version": 1,
        "content": [
            _adf_heading(3, "✅ リリース成功"),
            _adf_code_block("\n".join(lines)),
        ],
    }


# ---------------------------------------------------------------------------
# HTTP error helper
# ---------------------------------------------------------------------------

def _jira_error_detail(body: dict) -> str:
    lines = []
    for field, msg in body.get("errors", {}).items():
        lines.append(f"  field '{field}': {msg}")
    for msg in body.get("errorMessages", []):
        lines.append(f"  {msg}")
    return "\n".join(lines)


def _handle_error(response: requests.Response) -> None:
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}
    detail = _jira_error_detail(body)
    _log("ERROR", f"Jira API エラー: HTTP {status}")
    if status == 401:
        _log("ERROR", "認証エラー (401) — JIRA_EMAIL または JIRA_API_TOKEN を確認してください。")
    elif status == 403:
        _log("ERROR", f"権限エラー (403) — プロジェクト '{PROJECT_KEY}' への書き込み権限がありません。")
    elif status == 404:
        _log("ERROR", f"Not Found (404) — JIRA_URL '{JIRA_URL}' またはリソースが存在しません。")
    elif status == 400:
        body_str = str(body).lower()
        if "project" in body_str:
            _log("ERROR", f"プロジェクトキーエラー (400) — '{PROJECT_KEY}' が見つかりません。")
        elif "issuetype" in body_str:
            _log("ERROR", f"イシュータイプエラー (400) — '{ISSUE_TYPE}' がこのプロジェクトで使用できません。")
        else:
            _log("ERROR", f"リクエストエラー (400)")
    else:
        _log("ERROR", f"予期しないエラー ({status})")
    if detail:
        _log("ERROR", f"詳細:\n{detail}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Core API functions
# ---------------------------------------------------------------------------

def create_ticket(
    summary: str,
    description: str = "",
    version: str = "",
    release_url: str = "",
) -> str:
    """Create a Jira タスク and return the issue key (e.g. 'KAN-31').

    If description is empty, auto-generates from git log + release_url.
    Writes issue_key=<key> to $GITHUB_OUTPUT when running in GitHub Actions.
    Exits with code 1 on any failure (blocks the CI release upload).
    """
    validate_config()

    _log("INFO", "=" * 60)
    _log("INFO", f"対象バージョン    : {version or '(未指定)'}")
    _log("INFO", f"Jira URL          : {JIRA_URL}")
    _log("INFO", f"プロジェクト      : {PROJECT_KEY}")
    _log("INFO", f"ユーザー          : {JIRA_EMAIL}")
    _log("INFO", f"イシュータイプ    : {ISSUE_TYPE}")
    _log("INFO", f"サマリー          : {summary}")
    _log("INFO", f"GitHub Release URL: {release_url or '(未指定)'}")
    _log("INFO", "=" * 60)

    if description.strip():
        adf_desc = text_to_adf(description)
        _log("INFO", "説明: 引数から使用")
    else:
        _log("INFO", "説明: git log から自動生成")
        adf_desc = _build_release_adf(version, summary, release_url)

    payload = {
        "fields": {
            "project":     {"key": PROJECT_KEY},
            "summary":     summary,
            "description": adf_desc,
            "issuetype":   {"name": ISSUE_TYPE},
        }
    }

    _log("INFO", f"POST {JIRA_URL}/rest/api/3/issue ...")

    try:
        response = requests.post(
            f"{JIRA_URL}/rest/api/3/issue",
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            timeout=15,
        )
    except requests.exceptions.ConnectionError:
        _log("ERROR", f"Jira に接続できません: {JIRA_URL}")
        sys.exit(1)
    except requests.exceptions.Timeout:
        _log("ERROR", "Jira への接続がタイムアウトしました (15 秒)。")
        sys.exit(1)

    if response.status_code != 201:
        _handle_error(response)

    issue_key = response.json()["key"]
    issue_url = f"{JIRA_URL}/browse/{issue_key}"
    _log("INFO", "✓ Jira イシュー作成成功")
    _log("INFO", f"  Issue Key         : {issue_key}")
    _log("INFO", f"  URL               : {issue_url}")
    _log("INFO", f"  Version           : {version or '(未指定)'}")
    _log("INFO", f"  Description source: {'argument' if description.strip() else 'git log (auto)'}")

    # Expose issue key to subsequent GitHub Actions steps.
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"issue_key={issue_key}\n")
        _log("INFO", f"  GITHUB_OUTPUT     : issue_key={issue_key}")

    return issue_key


def update_description(
    issue_key: str,
    version: str = "",
    summary: str = "",
    release_url: str = "",
) -> None:
    """Overwrite the description of an existing Jira issue. Exits on failure."""
    validate_config()
    _log("INFO", f"説明を更新: {issue_key} ...")
    adf = _build_release_adf(version, summary, release_url)
    try:
        r = requests.put(
            f"{JIRA_URL}/rest/api/3/issue/{issue_key}",
            json={"fields": {"description": adf}},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            timeout=15,
        )
    except Exception as exc:
        _log("ERROR", f"説明更新中に例外: {exc}")
        sys.exit(1)

    if r.status_code == 204:
        _log("INFO", f"✓ {issue_key} の説明を更新しました")
    else:
        _log("ERROR", f"説明更新失敗: HTTP {r.status_code} — {r.text[:300]}")
        sys.exit(1)


def add_comment(
    issue_key: str,
    version: str = "",
    release_url: str = "",
    artifacts: list = None,
) -> None:
    """Add a release-success comment to a Jira issue. Exits on failure."""
    validate_config()
    _log("INFO", f"コメントを追加: {issue_key} ...")
    adf_body = _build_close_comment_adf(version, release_url, artifacts=artifacts)
    try:
        r = requests.post(
            f"{JIRA_URL}/rest/api/3/issue/{issue_key}/comment",
            json={"body": adf_body},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            timeout=15,
        )
    except Exception as exc:
        _log("ERROR", f"コメント追加中に例外: {exc}")
        sys.exit(1)

    if r.status_code == 201:
        _log("INFO", f"✓ コメント追加成功 (ID: {r.json().get('id', '?')})")
    else:
        _log("ERROR", f"コメント追加失敗: HTTP {r.status_code} — {r.text[:300]}")
        sys.exit(1)


def transition_to_done(issue_key: str, done_transition_id: str = "51") -> None:
    """Move issue_key to 完了 (Done). Exits on failure."""
    validate_config()
    _log("INFO", f"{issue_key} を 完了 に移行 ...")
    try:
        r = requests.post(
            f"{JIRA_URL}/rest/api/2/issue/{issue_key}/transitions",
            json={"transition": {"id": done_transition_id}},
            headers={"Content-Type": "application/json"},
            auth=(JIRA_EMAIL, JIRA_API_TOKEN),
            timeout=15,
        )
        if r.status_code == 204:
            _log("INFO", f"✓ {issue_key} を 完了 に移行しました")
        else:
            _log("ERROR", f"移行失敗: HTTP {r.status_code} — {r.text[:200]}")
            sys.exit(1)
    except Exception as exc:
        _log("ERROR", f"移行中に例外: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="create_jira_ticket.py",
        description="Mercari Inventory App — Jira イシュー作成・管理ツール",
    )
    p.add_argument("--summary", default="[MIA] 新規リリースタスク", metavar="TEXT",
                   help="イシューのサマリー")
    p.add_argument("--description", default="", metavar="TEXT",
                   help="イシューの説明（省略時は git log から自動生成）")
    p.add_argument("--version", default="", metavar="VERSION",
                   help="対象リリースバージョン (例: v1.4.3)")
    p.add_argument("--release-url", default="", metavar="URL",
                   help="GitHub Release URL")
    p.add_argument("--done", metavar="ISSUE_KEY",
                   help="完了に移行してリリースコメントを追加 (例: KAN-31)")
    p.add_argument("--update-description", metavar="ISSUE_KEY",
                   help="既存イシューの説明を更新 (例: KAN-31)")
    p.add_argument("--artifact", dest="artifacts", action="append", default=[],
                   metavar="NAME",
                   help="リリースアーティファクト名（繰り返し指定可）。コメントに記載される。")
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if args.done:
        _log("INFO", "=" * 60)
        _log("INFO", f"クローズ対象      : {args.done}")
        _log("INFO", f"バージョン        : {args.version or '(未指定)'}")
        _log("INFO", f"GitHub Release URL: {args.release_url or '(未指定)'}")
        _log("INFO", "=" * 60)
        _log("INFO", f"アーティファクト  : {args.artifacts or '(未指定)'}")
        transition_to_done(args.done)
        add_comment(
            args.done,
            version=args.version,
            release_url=args.release_url,
            artifacts=args.artifacts or None,
        )

    elif args.update_description:
        _log("INFO", "=" * 60)
        _log("INFO", f"説明更新対象      : {args.update_description}")
        _log("INFO", f"バージョン        : {args.version or '(未指定)'}")
        _log("INFO", f"GitHub Release URL: {args.release_url or '(未指定)'}")
        _log("INFO", "=" * 60)
        update_description(
            args.update_description,
            version=args.version,
            summary=args.summary,
            release_url=args.release_url,
        )

    else:
        create_ticket(
            args.summary,
            args.description,
            args.version,
            args.release_url,
        )
