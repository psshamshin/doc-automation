"""Сквозной прогон на придуманных данных: сбор, черновик, проверка, агент (заглушка), сравнение."""

import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline import cli as CLI
from pipeline import draft as DRAFT
from pipeline import product as P
from pipeline import validation as V
from tests.helpers import DEMO, ROOT

STUB = f"{sys.executable} {ROOT / 'tests' / 'stub_agent.py'}"
DATA_FILES = ["01_changelog.json", "02_code_diff.json", "02_reachability.json", "03_i18n_diff.json",
              "03_i18n_rows.json", "04_jira.json", "05_units.json", "06_docs_index.json", "07_review.json"]


def run(command, out, *extra):
    CLI.main([command, str(DEMO), "--out", str(out), *extra])


def digests(out: Path):
    files = [out / P.OUT_DATA / n for n in DATA_FILES] + [out / "report.md", out / "draft.md", out / V.FILE_NAME]
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


class CollectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, cls.out, True)
        run("collect", cls.out)
        cls.first = digests(cls.out)
        cls.units = json.loads((cls.out / P.OUT_DATA / "05_units.json").read_text(encoding="utf-8"))
        cls.result = cls.out / P.OUT_RESULT

    def text(self, *parts):
        return self.out.joinpath(*parts).read_text(encoding="utf-8")

    def test_output_structure(self):
        for name in ("report.md", "draft.md", V.FILE_NAME, "jira_query.txt"):
            self.assertTrue((self.out / name).is_file(), name)
        for n in DATA_FILES:
            self.assertTrue((self.out / P.OUT_DATA / n).is_file(), n)
        self.assertTrue((self.result / "MD_rus" / "docs" / "user-manual" / "100_Orders.md").is_file())

    def test_rule_decisions(self):
        got = {u["id"]: u["decision"]["action"]["value"] for u in self.units["units"]}
        self.assertEqual(got, {"GUI-101": "skip", "GUI-102": "skip", "GUI-103": "rn_and_check_doc",
                               "GUI-104": "rn_and_check_langs", "GUI-105": "doc_and_rn", "GUI-106": "doc_and_rn",
                               "NOTASK:0.10.0#7": "review"})

    def test_report(self):
        report = self.text("report.md")
        self.assertIn("# Изменения для релиза 0.2.0", report)
        self.assertIn("| GUI-104 | point2 (В5) | visible (Г1) | Д9 | RN + проверить список языков |", report)
        self.assertIn("не учитывается: техническая", report)

    def test_code_bumped_ui_version(self):
        for lang in ("rus", "eng"):
            self.assertIn("SECURE_CLIENT_VER = 0.10.0", self.text(P.OUT_RESULT, f"MD_{lang}", "docs", "administrator-manual", "055_Deploying.md"))
        self.assertEqual(self.text(P.OUT_RESULT, "trace.md").count("SECURE_CLIENT_VER: 0.8.0 -> 0.10.0"), 2)

    def test_release_notes_draft(self):
        rn = self.text(P.OUT_RESULT, "MD_rus", "docs", "release-notes", "020.md")
        self.assertIn(DRAFT.DRAFT_MARK, rn)
        self.assertIn("# [0.2.0] 01.03.2026 {-}", rn)
        self.assertRegex(rn, r"\| point-ui\s+\| 0\.10\.0\s+\|")
        self.assertIn("| demo-api   | 1.0.0      |", rn)
        self.assertIn('- **[Раздел "Заказы"]**: Добавлен фильтр заказов по дате <!-- GUI-105 -->', rn)
        self.assertIn("GUI-104", rn)
        self.assertNotIn("**Исправленные ошибки**", rn)   # единственное исправление еще не подтверждено
        self.assertIn("Не решено", rn)
        en = self.text(P.OUT_RESULT, "MD_eng", "docs", "release-notes", "020.md")
        self.assertIn('- **["Orders" section]**:', en)
        self.assertIn("TODO перевод", en)
        self.assertIn("- 020.md\n        - 010.md", self.text(P.OUT_RESULT, "MD_rus", "docs", "release-notes", ".pages"))

    def test_draft(self):
        d = self.text("draft.md")
        self.assertIn("| Новый релиз | 0.2.0 |", d)
        self.assertIn("| GUI-103 | agent | RN + проверить доку (В5, Г5, Д8) | **низкая** |", d)
        self.assertIn("- И3: видимость определена только по словам", d)
        self.assertIn("key in (GUI-101, GUI-102, GUI-103, GUI-104, GUI-105, GUI-106)", d)

    def test_no_absolute_paths_and_no_time(self):
        for n in DATA_FILES + ["../report.md", "../draft.md"]:
            self.assertNotIn(str(ROOT), (self.out / P.OUT_DATA / n).read_text(encoding="utf-8"), n)

    def test_rerun_is_identical(self):
        run("collect", self.out)
        self.assertEqual(digests(self.out), self.first)

    def test_compare(self):
        run("compare", self.out)
        cmp_ = json.loads(self.text(P.OUT_COMPARE, "compare.json"))
        self.assertIn("user-manual/100_Orders.md", cmp_["rus"]["only_reference"])
        self.assertIn("release-notes/020.md", cmp_["rus"]["both"])


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, True)
        run("collect", self.out)

    def approve(self, **changes):
        p = self.out / V.FILE_NAME
        text = p.read_text(encoding="utf-8").replace("approved: no", "approved: yes")
        for uid, d in changes.items():
            text = text.replace(f"{uid} | agent", f"{uid} | {d}")
        p.write_text(text, encoding="utf-8")

    def queue(self):
        return {i["id"]: i for i in json.loads((self.out / P.OUT_AGENT / "queue.json").read_text(encoding="utf-8"))["items"]}

    def test_refuses_without_approval(self):
        with self.assertRaises(SystemExit) as cm:
            run("agent", self.out, "--cmd", STUB)
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse((self.out / P.OUT_AGENT).exists())

    def test_dry_run_prepares_tasks_only(self):
        self.approve()
        run("agent", self.out, "--dry-run")
        q = self.queue()
        self.assertEqual(sorted(q), ["GUI-103__decide", "GUI-104__check_langs", "GUI-105__write_doc", "GUI-106__write_doc",
                                     "NOTASK_0.10.0_7__decide", "release_notes"])
        self.assertTrue(all(i["status"] == "prepared" for i in q.values()))
        task = (self.out / P.OUT_AGENT / "tasks" / "GUI-103__decide.md").read_text(encoding="utf-8")
        self.assertIn("И3: видимость определена только по словам", task)
        self.assertIn("answers/GUI-103__decide.json", task)
        patch = re.search(r"патч: `([^`]+)`", task).group(1)
        self.assertTrue(Path(patch).is_file() or (ROOT / patch).is_file(), patch)
        self.assertNotIn("{{", task.replace("{{count}}", ""))

    def test_agent_gets_only_what_rules_could_not_do(self):
        # человек сам решил обе спорные задачи: заданий decide нет
        self.approve(**{"GUI-103": "rn", "NOTASK:0.10.0#7": "skip"})
        run("agent", self.out, "--dry-run")
        kinds = sorted(i["kind"] for i in self.queue().values())
        self.assertEqual(kinds, ["check_langs", "release_notes", "write_doc", "write_doc"])

    def test_run_with_stub(self):
        self.approve()
        run("agent", self.out, "--cmd", STUB)
        q = self.queue()
        self.assertTrue(all(i["status"] == "done" for i in q.values()), q)
        final = json.loads((self.out / P.OUT_AGENT / "final_decisions.json").read_text(encoding="utf-8"))
        self.assertEqual(final["GUI-103"], "rn")
        self.assertEqual(final["NOTASK:0.10.0#7"], "skip")
        # решение агента попало в черновик release notes до задания release_notes
        rn = (self.out / P.OUT_RESULT / "MD_rus" / "docs" / "release-notes" / "020.md").read_text(encoding="utf-8")
        self.assertIn("**Исправленные ошибки**", rn)
        self.assertIn("<!-- GUI-103", rn)
        self.assertNotIn("Не решено", rn)
        self.assertIn("GUI-103: rn", (self.out / P.OUT_AGENT / "summary.md").read_text(encoding="utf-8"))

    def test_second_run_skips_done_items(self):
        self.approve()
        run("agent", self.out, "--cmd", STUB)
        run("agent", self.out, "--cmd", STUB + " fail")
        self.assertTrue(all(i["note"] == "уже выполнено раньше" for i in self.queue().values()))

    def test_failures_are_reported(self):
        self.approve(**{"GUI-103": "rn", "NOTASK:0.10.0#7": "skip"})
        for mode, msg in (("no_answer", "не записал файл ответа"), ("bad_json", "не является JSON"), ("fail", "кодом 3")):
            with self.assertRaises(SystemExit):
                run("agent", self.out, "--cmd", f"{STUB} {mode}", "--redo")
            notes = {i["note"] for i in self.queue().values()}
            self.assertTrue(any(msg in n for n in notes), (mode, notes))

    def test_finished_release_notes_are_not_overwritten(self):
        rn = self.out / P.OUT_RESULT / "MD_rus" / "docs" / "release-notes" / "020.md"
        rn.write_text("# [0.2.0] готовый текст\n", encoding="utf-8")
        run("collect", self.out)
        self.assertEqual(rn.read_text(encoding="utf-8"), "# [0.2.0] готовый текст\n")


class ProjectFilesTest(unittest.TestCase):
    def test_all_json_parse(self):
        for d in (P.KNOWLEDGE_DIR, "agent"):
            for p in sorted((ROOT / d).glob("*.json")):
                json.loads(p.read_text(encoding="utf-8"))

    def test_rule_changes_are_recorded(self):
        rules = json.loads((ROOT / P.RULES_FILE).read_text(encoding="utf-8"))
        doc = (ROOT / "docs" / "RULES.md").read_text(encoding="utf-8")
        for ch in rules["_changes"]:
            for key in ("n", "date", "rules", "old", "new", "reason", "where"):
                self.assertIn(key, ch)
            self.assertIn(f"### Изменение {ch['n']} ", doc)

    def test_every_agent_task_kind_has_a_prompt(self):
        from pipeline import agent as AG
        for kind in AG.KIND_ORDER + ["_common"]:
            self.assertTrue((ROOT / "agent" / "prompts" / f"{kind}.md").is_file(), kind)

    def test_knowledge_base_has_current_release(self):
        kn = json.loads((ROOT / P.RELEASES_FILE).read_text(encoding="utf-8"))
        by = {r["release"]: r for r in kn["releases"]}
        self.assertEqual(by["2.3.0"]["ui"], "2.11.0")
        self.assertEqual(by["2.2.0"]["ui"], "2.9.4")


if __name__ == "__main__":
    unittest.main()
