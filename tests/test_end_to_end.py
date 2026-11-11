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
              "03_i18n_rows.json", "03_i18n_usage.json", "04_jira.json", "05_units.json", "06_docs_index.json", "07_review.json"]


def run(command, out, *extra):
    if command == "agent" and "--cmd" in extra:
        extra = (*extra, "--allow-reference")   # в придуманных данных документация техписа лежит рядом
    CLI.main([command, str(DEMO), "--out", str(out), *extra])


def digests(out: Path):
    files = [out / P.OUT_DATA / n for n in DATA_FILES] + [out / "report.md", out / V.FILE_NAME]
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
        for name in ("report.md", V.FILE_NAME, "jira_query.txt"):
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
        self.assertIn('- **[Раздел "Заказы"]**: Добавлен фильтр заказов по дате <!-- GUI-105, источник: список изменений -->', rn)
        self.assertIn("GUI-104", rn)
        self.assertIn("**Исправленные ошибки**", rn)
        self.assertIn("Не решено, строки появятся после работы агента: NOTASK:0.10.0#7", rn)
        en = self.text(P.OUT_RESULT, "MD_eng", "docs", "release-notes", "020.md")
        self.assertIn('- **["Orders" section]**:', en)
        self.assertIn("TODO перевод", en)
        self.assertIn("- 020.md\n        - 010.md", self.text(P.OUT_RESULT, "MD_rus", "docs", "release-notes", ".pages"))

    def test_single_review_file(self):
        self.assertFalse((self.out / "draft.md").exists())
        d = self.text(V.FILE_NAME)
        self.assertIn("Проверено: [ ]", d)
        self.assertIn("| Новый релиз | 0.2.0 |", d)
        self.assertIn("версия интерфейса `SECURE_CLIENT_VER` 0.8.0 -> 0.10.0", d)
        self.assertIn("| GUI-105 | Добавлен фильтр заказов по дате | в доку + RN (В5, Г1, Д2) | - | [x] | [ ] | [ ] |", d)
        self.assertIn("| И1: продукт не определен | [ ] | [ ] | [x] |", d)
        self.assertIn("[docs/RULES.md](", d)
        self.assertIn("## Черновик release notes: требует просмотра", d)
        self.assertIn('| GUI-105 | Изменения | Раздел "Заказы" | Добавлен фильтр заказов по дате | список изменений |', d)
        self.assertNotIn("Коды в скобках", d)
        q = self.text("jira_query.txt")
        self.assertIn("\nkey in (GUI-101, GUI-102, GUI-103, GUI-104, GUI-105, GUI-106)\n", q)
        self.assertIn('project in (GUI) AND resolved >= "2026-01-10" AND resolved <= "2026-03-01 23:59" AND statusCategory = Done '
                      "AND key not in (GUI-101,", q)

    def test_no_absolute_paths_and_no_time(self):
        for n in DATA_FILES + ["../report.md", "../" + V.FILE_NAME]:
            self.assertNotIn(str(ROOT), (self.out / P.OUT_DATA / n).read_text(encoding="utf-8"), n)

    def test_rerun_is_identical(self):
        run("collect", self.out)
        self.assertEqual(digests(self.out), self.first)

    def test_compare(self):
        run("compare", self.out)
        cmp_ = json.loads(self.text(P.OUT_COMPARE, "compare.json"))
        self.assertIn("user-manual/100_Orders.md", cmp_["rus"]["only_reference"])
        self.assertIn("release-notes/020.md", cmp_["rus"]["both"])
        page = self.text(P.OUT_COMPARE, "compare.html")
        self.assertIn("user-manual/100_Orders.md", page)
        self.assertIn("<th>Пайплайн", page)


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, True)
        run("collect", self.out)

    def approve(self, **marks):
        """Ставит 'Проверено' и переставляет отметки: 0 - принять, 1 - отклонить, 2 - агенту."""
        p = self.out / V.FILE_NAME
        lines = p.read_text(encoding="utf-8").replace("Проверено: [ ]", "Проверено: [x]").splitlines()
        for uid, column in marks.items():
            for i, line in enumerate(lines):
                if line.startswith("|") and uid in line.split("|")[1]:
                    cells = line.rstrip("|").split("|")
                    cells[-3:] = [" [x] " if n == column else " [ ] " for n in range(3)]
                    lines[i] = "|".join(cells) + "|"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def queue(self):
        return {i["id"]: i for i in json.loads((self.out / P.OUT_AGENT / "queue.json").read_text(encoding="utf-8"))["items"]}

    def test_refuses_without_approval(self):
        with self.assertRaises(SystemExit) as cm:
            run("agent", self.out, "--cmd", STUB)
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse((self.out / P.OUT_AGENT).exists())

    def test_dry_run_prepares_tasks_only(self):
        self.approve(**{"GUI-103": 2})
        run("agent", self.out, "--dry-run")
        q = self.queue()
        self.assertEqual(sorted(q), ["GUI-103__decide", "GUI-104__check_langs", "GUI-105__write_doc", "GUI-106__write_doc",
                                     "NOTASK_0.10.0_7__decide", "release_notes"])
        self.assertTrue(all(i["status"] == "prepared" for i in q.values()))
        task = (self.out / P.OUT_AGENT / "tasks" / "GUI-103__decide.md").read_text(encoding="utf-8")
        self.assertIn("Решение правил: RN + проверить доку (В5, Г5, Д8).", task)
        self.assertIn("answers/GUI-103__decide.json", task)
        patch = re.search(r"патч: `([^`]+)`", task).group(1)
        self.assertTrue(Path(patch).is_file() or (ROOT / patch).is_file(), patch)
        self.assertNotIn("{{", task.replace("{{count}}", ""))

    def test_agent_gets_only_what_rules_could_not_do(self):
        # человек сам отклонил задачу с низкой достоверностью: заданий decide нет
        self.approve(**{"NOTASK:0.10.0#7": 1})
        run("agent", self.out, "--dry-run")
        kinds = sorted(i["kind"] for i in self.queue().values())
        self.assertEqual(kinds, ["check_langs", "release_notes", "write_doc", "write_doc"])

    def test_run_with_stub(self):
        self.approve(**{"GUI-103": 2})
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
        self.assertTrue((self.out / P.OUT_AGENT / "logs" / "GUI-103__decide.log").is_file())
        self.assertIn("GUI-103: rn", (self.out / P.OUT_AGENT / "summary.md").read_text(encoding="utf-8"))

    def test_second_run_skips_done_items(self):
        self.approve()
        run("agent", self.out, "--cmd", STUB)
        run("agent", self.out, "--cmd", STUB + " fail")
        self.assertTrue(all(i["note"] == "уже выполнено раньше" for i in self.queue().values()))

    def test_failures_are_reported(self):
        self.approve(**{"NOTASK:0.10.0#7": 1})
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


class ReferenceGuardTest(unittest.TestCase):
    """Агент не запускается, пока документация техписа лежит там, где он может ее прочитать."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data, self.out, self.ref = self.tmp / "data", self.tmp / "out", self.tmp / "elsewhere"
        shutil.copytree(DEMO, self.data)
        (self.data / "dataset.json").write_text(json.dumps({"doc_map": "doc_map.json", "jira_task_url": "", "release": "0.2.0"}), encoding="utf-8")

    def agent(self, *extra):
        CLI.main(["agent", str(self.data), "--out", str(self.out), "--approve", "--cmd", STUB, *extra])

    def test_refuses_when_reference_is_in_input(self):
        CLI.main(["collect", str(self.data), "--out", str(self.out)])
        with self.assertRaises(SystemExit) as cm:
            self.agent()
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse((self.out / P.OUT_AGENT / "queue.json").exists())

    def test_runs_when_reference_is_outside_then_compare_by_path(self):
        shutil.move(str(self.data / P.SLOT_DOCS_REFERENCE), str(self.ref))
        CLI.main(["collect", str(self.data), "--out", str(self.out)])
        self.agent()
        self.assertTrue((self.out / P.OUT_AGENT / "queue.json").is_file())
        CLI.main(["compare", str(self.data), "--out", str(self.out), "--reference", str(self.ref)])
        self.assertTrue((self.out / P.OUT_COMPARE / "compare.html").is_file())
        with self.assertRaises(SystemExit):     # после сравнения результаты лежат в output: агент снова не запускается
            self.agent("--redo")

    def test_preparing_tasks_is_always_allowed(self):
        CLI.main(["collect", str(self.data), "--out", str(self.out)])
        CLI.main(["agent", str(self.data), "--out", str(self.out), "--approve"])
        self.assertTrue((self.out / P.OUT_AGENT / "queue.json").is_file())


class CompareViewTest(unittest.TestCase):
    def test_three_columns(self):
        from pipeline import compare_view as CV
        base = "заголовок\nстарая строка\nудалят оба\nконец\n"
        gen = "заголовок\nновая строка\nконец\nдобавил пайплайн\n"
        ref = "заголовок\nновая строка\nконец\n"
        fr = CV.file_rows(base, gen, ref)
        self.assertEqual(fr["stats"], {"same": 2, "diff": 0, "gen": 1, "ref": 0})
        changed = next(r for r in fr["rows"] if r[3] == "chg")
        self.assertEqual(changed[0], "<mark>старая</mark> строка")       # в базе выделено замененное слово
        self.assertEqual(changed[1], "<mark>новая</mark> строка")
        self.assertEqual(changed[1], changed[2])
        self.assertEqual([r[3] for r in fr["rows"]][-1], "add")
        self.assertEqual(fr["rows"][-1][4], "pad")

    def test_different_edits_of_same_line(self):
        from pipeline import compare_view as CV
        fr = CV.file_rows("версия 1\n", "версия 2\n", "версия 3\n")
        self.assertEqual(fr["stats"], {"same": 0, "diff": 1, "gen": 0, "ref": 0})

    def test_html_is_escaped_and_page_is_self_contained(self):
        from pipeline import compare_view as CV
        fr = CV.file_rows("a <b>\n", "a <script>x</script>\n", "a <b>\n")
        self.assertIn("&lt;", fr["rows"][0][1])
        self.assertNotIn("<script", fr["rows"][0][1])
        page = CV.render_page({"rus": [{"path": "p.md", "kind": "gen", **fr}]}, "Сравнение")
        self.assertNotIn("<script>x</script>", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_new_file_only_at_reference(self):
        from pipeline import compare_view as CV
        fr = CV.file_rows(None, None, "новая страница\n")
        self.assertTrue(fr["base_missing"] and fr["gen_missing"] and not fr["ref_missing"])
        self.assertEqual(fr["stats"]["ref"], 1)


class ManualAgentTest(unittest.TestCase):
    """Агент запускается вручную (чат в редакторе): пайплайн готовит задания и подхватывает готовые ответы."""

    def test_answers_written_by_hand_are_picked_up(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        run("collect", out)
        run("agent", out, "--approve")                       # агент не настроен: только задания
        answers = out / P.OUT_AGENT / "answers"
        answers.mkdir(parents=True, exist_ok=True)
        (answers / "NOTASK_0.10.0_7__decide.json").write_text(json.dumps(
            {"item": "NOTASK_0.10.0_7__decide", "status": "done", "summary": "сборка, пользователю не видно", "decision": "skip"},
            ensure_ascii=False), encoding="utf-8")
        run("agent", out, "--approve")
        q = {i["id"]: i for i in json.loads((out / P.OUT_AGENT / "queue.json").read_text(encoding="utf-8"))["items"]}
        self.assertEqual(q["NOTASK_0.10.0_7__decide"]["status"], "done")
        self.assertEqual(q["GUI-105__write_doc"]["status"], "prepared")
        final = json.loads((out / P.OUT_AGENT / "final_decisions.json").read_text(encoding="utf-8"))
        self.assertEqual(final["NOTASK:0.10.0#7"], "skip")


class NoAgentConfiguredTest(unittest.TestCase):
    def test_tasks_are_prepared_when_no_agent_command(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        run("collect", out)
        run("agent", out, "--approve")
        q = json.loads((out / P.OUT_AGENT / "queue.json").read_text(encoding="utf-8"))
        self.assertFalse(q["agent_available"])
        self.assertTrue(all(i["status"] == "prepared" for i in q["items"]))


if __name__ == "__main__":
    unittest.main()
