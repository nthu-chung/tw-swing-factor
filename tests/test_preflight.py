# -*- coding: utf-8 -*-
"""公開 repo preflight 檢查的離線回歸測試。

這支測試守兩件事:

1. **檢查真的會擋。** 每條規則都用假的 tracked 清單／暫存檔驗證會命中,
   避免 preflight 退化成永遠印 ✅ 的裝飾品。
2. **preflight 自己不洩漏。** 命中時只能吐規則與行號;若有人「順手」把
   match 內容加進 detail,`test_findings_never_echo_secret_values` 會紅。

全部離線:不呼叫 FinMind／TWSE／TPEx,也不需要真的建 git repo
(`run_preflight` 的 `tracked` 參數可注入)。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import preflight


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SecretFilenameTest(unittest.TestCase):
    def test_common_secret_filenames_are_flagged(self):
        tracked = [
            ".env",
            ".env.production",
            "config/secrets.json",
            "certs/server.pem",
            "deploy/id_rsa",
            "keys/client.p12",
        ]
        rules = preflight.check_secret_filenames(tracked)
        self.assertEqual(len(rules), len(tracked))
        self.assertTrue(all(f.level == "fail" for f in rules))
        self.assertEqual({f.rule for f in rules}, {"secret_filename"})

    def test_env_example_and_normal_sources_pass(self):
        tracked = [".env.example", "config.py", "README.md", "tests/test_x.py"]
        self.assertEqual(preflight.check_secret_filenames(tracked), [])


class TrackedContentTest(unittest.TestCase):
    def test_private_key_header_in_tracked_file_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "notes.md",
                   "說明\n-----BEGIN RSA PRIVATE KEY-----\n")
            findings = preflight.check_tracked_contents(["notes.md"], root)
            self.assertEqual([f.rule for f in findings], ["private_key_pem"])
            self.assertEqual(findings[0].line, 2)

    def test_filled_token_assignment_is_flagged_but_empty_template_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "leaky.txt", "FINMIND_TOKEN=" + "a" * 40 + "\n")
            _write(root, "clean.txt", "FINMIND_TOKEN=\n")
            leaky = preflight.check_tracked_contents(["leaky.txt"], root)
            clean = preflight.check_tracked_contents(["clean.txt"], root)
            self.assertEqual([f.rule for f in leaky], ["filled_token_assignment"])
            self.assertEqual(clean, [])

    def test_runtime_token_loader_is_not_treated_as_a_secret(self):
        """變數名稱或函式呼叫不是 token 值，不能讓正常設定檔永遠紅燈。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "config.py",
                "FINMIND_TOKEN = _load_finmind_token()\n"
                "OTHER_TOKEN = os.getenv('OTHER_TOKEN', '')\n",
            )
            self.assertEqual(
                preflight.check_tracked_contents(["config.py"], root),
                [],
            )

    def test_findings_never_echo_secret_values(self):
        """命中訊息不得含被比對到的內容 —— CI log 是公開的。"""
        secret = "ghp_" + "Z" * 36
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "leaky.md", f"token: {secret}\n")
            findings = preflight.check_tracked_contents(["leaky.md"], root)
            self.assertTrue(findings)
            for f in findings:
                self.assertNotIn(secret, f.detail)
                self.assertNotIn(secret, f.render())

    def test_untracked_file_on_disk_is_ignored(self):
        """preflight 問的是「會被 push 出去的是什麼」,不是工作樹有什麼。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "local_only.md", "-----BEGIN PRIVATE KEY-----\n")
            self.assertEqual(preflight.check_tracked_contents([], root), [])


class DataArtifactTest(unittest.TestCase):
    def test_cache_and_output_artifacts_are_flagged(self):
        findings = preflight.check_data_artifacts([
            "_cache/price__2330.pkl",
            "outputs/backtest_phase_summary.csv",
            "outputs/rotation_trades.csv",
            "research_scratch.parquet",
        ])
        self.assertEqual(
            {f.rule for f in findings},
            {"tracked_cache_artifact", "tracked_output_artifact",
             "tracked_data_artifact"},
        )
        self.assertEqual(len(findings), 4)

    def test_deliberate_output_fixtures_are_allowed(self):
        """Markdown 研究紀錄、候選池 fixture 與凍結 manifest 是刻意進版控的。"""
        allowed = [
            "outputs/README.md",
            "outputs/WEIGHT_FIX_REPORT.md",
            "outputs/universe_top300.json",
            "outputs/FROZEN_MANIFEST_2026-07-24.json",
        ]
        self.assertEqual(preflight.check_data_artifacts(allowed), [])


class RequiredFileTest(unittest.TestCase):
    def test_missing_public_file_fails_but_untracked_only_warns(self):
        """檔案不存在是 fail;存在但還沒 git add 只是 commit 前的中間狀態。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "README.md", "x")          # 存在但下面不列入 tracked
            findings = preflight.check_required_files([], root)
            missing = [f for f in findings if f.rule == "missing_public_file"]
            untracked = [f for f in findings if f.rule == "untracked_public_file"]
            self.assertTrue(missing)
            self.assertTrue(all(f.level == "fail" for f in missing))
            self.assertEqual([f.path for f in untracked], ["README.md"])
            self.assertEqual(untracked[0].level, "warn")

    def test_fully_tracked_repo_has_no_required_file_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in preflight.REQUIRED_PUBLIC_FILES:
                _write(root, rel, "x")
            self.assertEqual(
                preflight.check_required_files(preflight.REQUIRED_PUBLIC_FILES, root),
                [],
            )


class GitignoreTest(unittest.TestCase):
    def test_missing_pattern_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, ".gitignore", "_cache/\n.env\n")
            findings = preflight.check_gitignore(root)
            self.assertTrue(findings)
            self.assertEqual({f.rule for f in findings}, {"gitignore_gap"})

    def test_absent_gitignore_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = preflight.check_gitignore(Path(tmp))
            self.assertEqual([f.rule for f in findings], ["missing_gitignore"])


class EnvExampleTest(unittest.TestCase):
    def test_filled_token_in_template_is_flagged_without_echoing_it(self):
        secret = "b" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, ".env.example", f"# 樣板\nFINMIND_TOKEN={secret}\n")
            findings = preflight.check_env_example(root)
            self.assertEqual([f.rule for f in findings], ["env_example_has_value"])
            self.assertNotIn(secret, findings[0].render())

    def test_empty_template_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, ".env.example",
                   "FINMIND_TOKEN=\nSWING_SNAPSHOT_END=2026-06-22\n")
            self.assertEqual(preflight.check_env_example(root), [])


class OwnerDecisionTest(unittest.TestCase):
    def test_absent_license_is_owner_decision_not_failure(self):
        """授權條款由 owner 決定;preflight 提醒但不代為決定,也不因此擋 CI。"""
        with tempfile.TemporaryDirectory() as tmp:
            items = preflight.owner_decisions(Path(tmp))
            self.assertEqual([f.rule for f in items], ["license_undecided"])
            self.assertEqual(items[0].level, "owner_decision")

    def test_present_license_clears_the_reminder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "LICENSE", "MIT")
            self.assertEqual(preflight.owner_decisions(root), [])


class ThisRepoTest(unittest.TestCase):
    """對本 repo 目前追蹤中的檔案實跑一次,確保 preflight 是綠的。"""

    def test_repo_has_no_preflight_failures(self):
        findings = preflight.run_preflight()
        failures = [f.render() for f in findings if f.level == "fail"]
        self.assertEqual(failures, [], "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
