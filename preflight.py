# -*- coding: utf-8 -*-
"""公開 repo 的 release / preflight 檢查(離線、唯讀)。

這支腳本回答的問題只有一個:**現在 push 到公開 GitHub 會不會外洩或誤導。**
它不驗證策略正確性(那是 `tests/` 與研究紀律的事),也不呼叫任何網路端點。

檢查五類:

1. Git 追蹤中的檔名是否命中常見密鑰／私鑰型態(`.env`、`*.pem`、`id_rsa` …)。
2. Git 追蹤中的文字檔是否含私鑰 PEM 標頭或已填值的 token 指派。
3. `_cache/` 與 `outputs/` 的資料產物是否被追蹤(只有少數刻意的 fixture 例外)。
4. 公開 repo 必要文件是否存在、`.gitignore` 是否真的擋得住、`.env.example` 是否空值。
5. LICENSE、個人使用附加許可、商業政策、CLA 與風險聲明是否一起被追蹤。

**設計鐵則:任何命中只印「規則 + 檔案 + 行號」,永遠不印比對到的內容。**
把疑似 token 印進 CI log 等於再洩一次;preflight 自己不能變成洩漏管道。

用法:

    PYTHONPATH=. .venv/bin/python preflight.py          # 失敗回傳 exit code 1
    PYTHONPATH=. .venv/bin/python preflight.py --json   # 給 CI 或工具解析
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

ROOT = Path(__file__).resolve().parent

# --- 1. 密鑰檔名型態 -------------------------------------------------------
# 用 basename 比對;`config/secrets.json` 這種放在子目錄的一樣要擋。
SECRET_FILENAME_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.ovpn",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials*.json",
    "secrets*.json",
    "service-account*.json",
    "*.token",
)
# `.env.example` 是刻意公開的無值樣板,`*.key` 誤傷的說明檔也在此白名單。
SECRET_FILENAME_ALLOWLIST = (
    ".env.example",
)

# --- 2. 內容型態 -----------------------------------------------------------
# 只留「幾乎不可能是文件示範」的高信度型態,降低假警報。
# 注意:pattern 只用來判斷命中,絕不把 match 內容放進輸出。
CONTENT_RULES = (
    ("private_key_pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("pgp_private_key", re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
    ("putty_private_key", re.compile(r"PuTTY-User-Key-File-\d")),
    # FINMIND_TOKEN=<有值> / FINMIND_TOKEN: "<有值>";空值樣板不算命中。
    ("filled_token_assignment",
     re.compile(r"""(?ix)
        \b(FINMIND_TOKEN|API[_-]?KEY|ACCESS[_-]?TOKEN|SECRET[_-]?KEY|
           AWS_SECRET_ACCESS_KEY|GITHUB_TOKEN)\b
        \s*[:=]\s*
        (?:["'][A-Za-z0-9_\-\.]{16,}["']|[A-Za-z0-9_\-\.]{16,})
        (?=\s*(?:[,;#}]|$))
     """)),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key_json", re.compile(r'"private_key"\s*:\s*"-----BEGIN')),
)
# preflight 自己與稽核文件會「描述」這些型態,不能自我命中。
CONTENT_SCAN_SKIP = (
    "preflight.py",
    "tests/test_preflight.py",
)
CONTENT_SCAN_MAX_BYTES = 2_000_000
TEXT_SUFFIXES = {
    "", ".py", ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg",
    ".ini", ".sh", ".example", ".gitignore",
}

# --- 3. 資料產物 -----------------------------------------------------------
# `_cache/` 一律不得追蹤。`outputs/` 只放研究紀錄(Markdown)與少數刻意的 fixture:
#   - universe_top*.json:重現 legacy static universe 對照組所需
#   - FROZEN_MANIFEST_*.json:凍結規則,依定義不可覆寫,必須進版控
#   - holdout_ledger.jsonl(+ .checkpoint.json)、forward_test_runs.jsonl:
#     **稽核紀錄**,不是資料產物。它們記的是「這段 holdout 被誰看過幾次」;
#     不進版控的話換一台 clone 或一個 `rm` 就靜靜回到 fresh(見
#     evaluation/holdout.py 的設計決定 4),而那正是它們存在的唯一理由。
DATA_ARTIFACT_DIRS = ("_cache/",)
OUTPUT_DIR_PREFIX = "outputs/"
OUTPUT_ALLOWLIST = (
    "outputs/*.md",
    "outputs/universe_top*.json",
    "outputs/FROZEN_MANIFEST_*.json",
    "outputs/holdout_ledger.jsonl",
    "outputs/holdout_ledger.jsonl.checkpoint.json",
    "outputs/forward_test_runs.jsonl",
)
# 資料產物副檔名:任何位置被追蹤都視為誤提交(測試 fixture 除外)。
DATA_ARTIFACT_SUFFIXES = (
    ".csv", ".pkl", ".pickle", ".parquet", ".feather", ".h5", ".hdf5",
    ".db", ".sqlite", ".sqlite3", ".log", ".zip", ".gz", ".xlsx",
)
DATA_ARTIFACT_ALLOWLIST = (
    "tests/fixtures/*",
)

# --- 4. 公開文件與 gitignore ----------------------------------------------
REQUIRED_PUBLIC_FILES = (
    "README.md",
    "LICENSE",
    "ADDITIONAL_PERMISSION.md",
    "COMMERCIAL_LICENSE.md",
    "CONTRIBUTING.md",
    "CLA.md",
    "DISCLAIMER.md",
    "DATA_LICENSE.md",
    "TRADEMARKS.md",
    "SPONSORING.md",
    "AGENTS.md",
    "ARCHITECTURE.md",
    "DATA_SOURCES.md",
    "STRATEGY_REGISTRY.md",
    "RESEARCH_OPERATING_PROTOCOL.md",
    "PUBLIC_REPO_AUDIT.md",
    "TAIWAN_MARKET_RULES.md",
    ".env.example",
    ".gitignore",
    "requirements.txt",
    "outputs/README.md",
    "preflight.py",
    "tests/test_preflight.py",
    ".github/workflows/ci.yml",
    ".github/pull_request_template.md",
)
# 這些 pattern 必須真的在 .gitignore 出現,否則乾淨 clone 很容易誤提交。
REQUIRED_GITIGNORE_PATTERNS = (
    "_cache/",
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "__pycache__/",
    ".venv/",
)


@dataclass(frozen=True)
class Finding:
    """一筆檢查結果。

    `detail` 只描述「命中哪一條規則」,不得含檔案內容;`line` 讓人自己去看。

    兩個 level 的分工:

    - `fail`:公開出去就會出事(密鑰、資料產物、文件根本不存在)。exit code 1。
    - `warn`:目前工作樹狀態下的待辦,commit 之後就會消失(例如必要文件還沒
      `git add`)。**不擋 CI** —— 在 CI 上檔案本來就已經 commit,這條自然不會亮;
      在本機亮起來是提醒,不是錯誤。
    """

    level: str      # "fail" | "warn"
    rule: str
    path: str
    detail: str
    line: int = 0

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{self.rule}] {where} — {self.detail}"


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def list_tracked_files(root: Path = ROOT) -> List[str]:
    """回傳 git 追蹤中的檔案(POSIX 相對路徑)。

    用 `git ls-files` 而不是走檔案系統:preflight 要回答的是「**會被 push 出去的**
    是什麼」,不是工作樹裡有什麼。未追蹤的本機資料不該讓 preflight 失敗。
    """
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def check_secret_filenames(tracked: Sequence[str]) -> List[Finding]:
    findings = []
    for path in tracked:
        name = Path(path).name
        if name in SECRET_FILENAME_ALLOWLIST:
            continue
        if _matches_any(name, SECRET_FILENAME_PATTERNS):
            findings.append(Finding(
                level="fail", rule="secret_filename", path=path,
                detail="檔名命中常見密鑰／私鑰型態,不應被 git 追蹤",
            ))
    return findings


def check_tracked_contents(tracked: Sequence[str], root: Path = ROOT) -> List[Finding]:
    """掃描追蹤中的文字檔是否含私鑰或已填值的 token。

    只回報規則與行號。**不要改成把 match 印出來** —— 那會讓 CI log 變成新的洩漏點。
    """
    findings = []
    for path in tracked:
        if path in CONTENT_SCAN_SKIP:
            continue
        full = root / path
        if not full.is_file():
            continue
        if full.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            if full.stat().st_size > CONTENT_SCAN_MAX_BYTES:
                continue
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue        # 讀不動或非 UTF-8 文字:交給託管平台的 secret scanning
        for lineno, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in CONTENT_RULES:
                if pattern.search(line):
                    findings.append(Finding(
                        level="fail", rule=rule, path=path, line=lineno,
                        detail="追蹤中的檔案命中密鑰型態(內容刻意不顯示)",
                    ))
    return findings


def check_data_artifacts(tracked: Sequence[str]) -> List[Finding]:
    findings = []
    for path in tracked:
        if any(path.startswith(prefix) for prefix in DATA_ARTIFACT_DIRS):
            findings.append(Finding(
                level="fail", rule="tracked_cache_artifact", path=path,
                detail="原始資料快取不得進版控(體積大且會與 snapshot 不一致)",
            ))
            continue
        if path.startswith(OUTPUT_DIR_PREFIX):
            if not _matches_any(path, OUTPUT_ALLOWLIST):
                findings.append(Finding(
                    level="fail", rule="tracked_output_artifact", path=path,
                    detail="outputs/ 只保留 Markdown 研究紀錄與候選池／凍結 fixture",
                ))
            continue
        if _matches_any(path, DATA_ARTIFACT_ALLOWLIST):
            continue
        if Path(path).suffix.lower() in DATA_ARTIFACT_SUFFIXES:
            findings.append(Finding(
                level="fail", rule="tracked_data_artifact", path=path,
                detail="資料產物副檔名不應進版控;需要 fixture 請放 tests/fixtures/",
            ))
    return findings


def check_required_files(tracked: Sequence[str], root: Path = ROOT) -> List[Finding]:
    """必要公開文件要同時「存在」且「被追蹤」。

    只檢查存在不夠 —— 檔案在本機但沒 git add,clone 出去的人一樣讀不到。
    但「還沒 add」是 commit 前的正常中間狀態,所以降為 warn:真正的缺件(檔案
    根本不存在)才是 fail。
    """
    tracked_set = set(tracked)
    findings = []
    for rel in REQUIRED_PUBLIC_FILES:
        if not (root / rel).is_file():
            findings.append(Finding(
                level="fail", rule="missing_public_file", path=rel,
                detail="公開 repo 必要文件不存在",
            ))
        elif rel not in tracked_set:
            findings.append(Finding(
                level="warn", rule="untracked_public_file", path=rel,
                detail="檔案存在但尚未 git add,公開前必須提交,否則 clone 後讀不到",
            ))
    return findings


def check_gitignore(root: Path = ROOT) -> List[Finding]:
    path = root / ".gitignore"
    if not path.is_file():
        return [Finding(level="fail", rule="missing_gitignore", path=".gitignore",
                        detail="沒有 .gitignore,快取與密鑰隨時可能被誤提交")]
    lines = {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()}
    findings = []
    for pattern in REQUIRED_GITIGNORE_PATTERNS:
        if pattern not in lines:
            findings.append(Finding(
                level="fail", rule="gitignore_gap", path=".gitignore",
                detail=f"缺少必要忽略規則: {pattern}",
            ))
    return findings


def check_env_example(root: Path = ROOT) -> List[Finding]:
    """`.env.example` 是唯一被追蹤的 env 檔,所以它必須永遠是空值樣板。"""
    path = root / ".env.example"
    if not path.is_file():
        return [Finding(level="fail", rule="missing_env_example", path=".env.example",
                        detail="缺少無密鑰的環境變數樣板")]
    findings = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().upper().endswith(("TOKEN", "KEY", "SECRET", "PASSWORD")):
            if value.strip().strip("\"'"):
                findings.append(Finding(
                    level="fail", rule="env_example_has_value", path=".env.example",
                    line=lineno,
                    detail="樣板的密鑰欄位必須留空(值刻意不顯示)",
                ))
    return findings


def run_preflight(root: Path = ROOT, tracked: Sequence[str] | None = None) -> List[Finding]:
    """跑完所有檢查。`tracked` 可注入,讓測試不必真的建一個 git repo。"""
    files = list(tracked) if tracked is not None else list_tracked_files(root)
    findings: List[Finding] = []
    findings += check_secret_filenames(files)
    findings += check_tracked_contents(files, root)
    findings += check_data_artifacts(files)
    findings += check_required_files(files, root)
    findings += check_gitignore(root)
    findings += check_env_example(root)
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公開 repo preflight 檢查(離線)")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 給工具解析")
    args = parser.parse_args(argv)

    findings = run_preflight()
    failures = [f for f in findings if f.level == "fail"]
    warnings = [f for f in findings if f.level == "warn"]

    if args.json:
        print(json.dumps(
            {"failures": [asdict(f) for f in failures],
             "warnings": [asdict(f) for f in warnings]},
            ensure_ascii=False, indent=2,
        ))
        return 1 if failures else 0

    print("=" * 78)
    print("  public repo preflight(離線;命中內容刻意不顯示)")
    print("=" * 78)
    if failures:
        print(f"\n❌ {len(failures)} 項必須修正:\n")
        for f in failures:
            print(f"  {f.render()}")
    else:
        print("\n✅ 密鑰檔名／內容、資料產物追蹤、必要文件、.gitignore、"
              ".env.example 全數通過。")
    if warnings:
        print(f"\n⚠️  {len(warnings)} 項 commit 前待辦(不擋 CI):\n")
        for f in warnings:
            print(f"  {f.render()}")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
