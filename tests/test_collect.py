"""Сбор: структура результатов, повторяемость, сравнение с эталоном, защита эталона от агента."""

import hashlib
import json
import re
import shutil
import unittest
from pathlib import Path

from pipeline import compare_view as CV
from pipeline import product as P
from pipeline import validation as V
from tests.helpers import DEMO, ROOT, STUB, Workdir, mark_review

DATA_FILES = ["01_changelog.json", "02_code_diff.json", "02_reachability.json", "03_i18n_diff.json", "03_i18n_rows.json",
              "03_i18n_usage.json", "04_jira.json", "05_units.json", "05_stale_texts.json", "05_doc_checks.json",
              "06_docs_index.json", "07_review.json"]


def digests(out: Path):
    files = [out / P.OUT_DATA / n for n in DATA_FILES] + [out / V.FILE_NAME]
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


class CollectTest(Workdir):
    def setUp(self):
        super().setUp()
        self.cli("collect")

    def test_outputs(self):
        for name in (V.FILE_NAME, "checks.md", "jira_query.txt"):
            self.assertTrue((self.out / name).is_file(), name)
        for n in DATA_FILES:
            self.assertTrue((self.out / P.OUT_DATA / n).is_file(), n)
        self.assertFalse((self.out / "report.md").exists())

    def test_rule_decisions(self):
        units = json.loads((self.out / P.OUT_DATA / "05_units.json").read_text(encoding="utf-8"))["units"]
        self.assertEqual({u["id"]: u["decision"]["action"]["value"] for u in units},
                         {"GUI-101": "skip", "GUI-102": "skip", "GUI-103": "rn_and_check_doc", "GUI-104": "rn_and_check_langs",
                          "GUI-105": "doc_and_rn", "GUI-106": "doc_and_rn", "NOTASK:0.10.0#7": "review"})

    def test_review_file(self):
        review = (self.out / V.FILE_NAME).read_text(encoding="utf-8")
        for text in ("Проверено: [ ]", "| Новый релиз | 0.2.0 |", "версия интерфейса `SECURE_CLIENT_VER` 0.8.0 -> 0.10.0",
                     "| GUI-105 | Добавлен фильтр заказов по дате | в доку + RN (В5, Г1, Д2) | - | [x] | [ ] | [ ] |",
                     "| И1: продукт не определен | [ ] | [ ] | [x] |", "[docs/RULES.md](", "## Release notes",
                     "## Сквозные проверки документации", "## Названия, которых больше нет в интерфейсе"):
            self.assertIn(text, review)

    def test_code_bumps_the_ui_version_in_both_languages(self):
        for lang in ("rus", "eng"):
            page = self.out / P.OUT_RESULT / f"MD_{lang}/docs/administrator-manual/055_Deploying.md"
            self.assertIn("SECURE_CLIENT_VER = 0.10.0", page.read_text(encoding="utf-8"))

    def test_no_release_notes_file_before_the_agent(self):
        self.assertFalse((self.out / P.OUT_RESULT / "MD_rus/docs/release-notes/020.md").exists())

    def test_results_are_the_same_on_rerun_and_do_not_hold_absolute_paths(self):
        first = digests(self.out)
        self.cli("collect")
        self.assertEqual(digests(self.out), first)
        for n in DATA_FILES:
            self.assertNotIn(str(ROOT), (self.out / P.OUT_DATA / n).read_text(encoding="utf-8"), n)

    def test_human_marks_survive_rerun(self):
        mark_review(self.out, **{"GUI-103": 2})
        self.cli("collect")
        self.assertIn("Проверено: [x]", (self.out / V.FILE_NAME).read_text(encoding="utf-8"))
        self.assertTrue(V.parse((self.out / V.FILE_NAME).read_text(encoding="utf-8"))["marks"]["GUI-103"] == "agent")

    def test_force_removes_earlier_agent_work(self):
        mark_review(self.out)
        self.cli("agent")
        self.assertTrue((self.out / P.OUT_WORKSPACE).exists())
        self.cli("collect", "--force")
        self.assertFalse((self.out / P.OUT_WORKSPACE).exists())

    def test_compare_page(self):
        self.cli("compare")
        cmp_ = json.loads((self.out / P.OUT_COMPARE / "compare.json").read_text(encoding="utf-8"))
        self.assertIn("user-manual/100_Orders.md", cmp_["rus"]["only_reference"])
        page = (self.out / P.OUT_COMPARE / "compare.html").read_text(encoding="utf-8")
        self.assertIn("user-manual/100_Orders.md", page)
        self.assertIn("<th>Пайплайн", page)


class ReferenceGuardTest(Workdir):
    """Агент не запускается, пока документация техписа лежит там, где он может ее прочитать."""

    def test_agent_command_is_refused_while_reference_is_in_the_project(self):
        self.cli("collect")
        mark_review(self.out)
        with self.assertRaises(SystemExit) as cm:
            self.cli("agent", "--cmd", STUB)
        self.assertEqual(cm.exception.code, 2)
        self.cli("agent")                                          # подготовка заданий разрешена всегда
        self.assertTrue((self.out / P.OUT_WORKSPACE / "tasks").is_dir())

    def test_reference_kept_outside_can_be_given_to_compare(self):
        ref = self.tmp / "elsewhere"
        shutil.move(str(self.data / P.SLOT_DOCS_REFERENCE), str(ref))
        self.cli("collect")
        mark_review(self.out)
        self.cli("agent", "--cmd", STUB)
        self.cli("compare", "--reference", str(ref))
        self.assertTrue((self.out / P.OUT_COMPARE / "compare.html").is_file())
        with self.assertRaises(SystemExit):                        # результаты сравнения лежат в output: агент снова не запускается
            self.cli("agent", "--cmd", STUB, "--redo")


class CompareViewTest(unittest.TestCase):
    def test_three_columns(self):
        fr = CV.file_rows("заголовок\nстарая строка\nудалят оба\nконец\n", "заголовок\nновая строка\nконец\nдобавил пайплайн\n",
                          "заголовок\nновая строка\nконец\n")
        self.assertEqual(fr["stats"], {"same": 2, "diff": 0, "gen": 1, "ref": 0})
        changed = next(r for r in fr["rows"] if r[3] == "chg")
        self.assertEqual((changed[0], changed[1]), ("<mark>старая</mark> строка", "<mark>новая</mark> строка"))
        self.assertEqual(CV.file_rows("версия 1\n", "версия 2\n", "версия 3\n")["stats"]["diff"], 1)

    def test_page_is_escaped_and_self_contained(self):
        fr = CV.file_rows("a <b>\n", "a <script>x</script>\n", "a <b>\n")
        page = CV.render_page({"rus": [{"path": "p.md", "kind": "gen", **fr}]}, "Сравнение")
        self.assertNotIn("<script>x</script>", page)
        self.assertNotIn("http", page)


class ProjectFilesTest(unittest.TestCase):
    def test_knowledge_files_parse(self):
        for p in sorted((ROOT / P.KNOWLEDGE_DIR).glob("*.json")):
            json.loads(p.read_text(encoding="utf-8"))

    def test_agent_prompts_cover_every_task_kind(self):
        from pipeline.agent import tasks
        rules = (ROOT / "agent" / "prompts" / "workspace_agents.md").read_text(encoding="utf-8")
        for kind in tasks.KIND_ORDER:
            self.assertIn(f"## {kind}", rules)
        self.assertTrue((ROOT / "agent" / "prompts" / "task.md").is_file())

    def test_every_rule_group_is_documented(self):
        doc = (ROOT / "docs" / "RULES.md").read_text(encoding="utf-8")
        for rid in ("А1", "Б1", "В1", "Г1", "Д2", "Е1", "И1", "К1", "Ж1", "З1"):
            self.assertIn(f"| {rid} |", doc)


if __name__ == "__main__":
    unittest.main()
