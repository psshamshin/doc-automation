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
        self.assertIn("NOTASK:0.10.0#7, источник: список изменений, ожидает решения", rn)
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
        self.ws = self.out / P.OUT_WORKSPACE
        self.page = Path("MD_rus") / "docs" / "user-manual" / "100_Orders.md"

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
        return json.loads((self.out / P.OUT_AGENT / "queue.json").read_text(encoding="utf-8"))

    def items(self):
        return {i["id"]: i for i in self.queue()["items"]}

    def summary(self):
        return (self.out / P.OUT_AGENT / "summary.md").read_text(encoding="utf-8")

    def test_refuses_without_review(self):
        with self.assertRaises(SystemExit) as cm:
            run("agent", self.out, "--cmd", STUB)
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(self.ws.exists())

    def test_workspace_has_only_what_tasks_need(self):
        self.approve(**{"GUI-103": 2})
        run("agent", self.out)
        self.assertEqual(sorted(p.name for p in self.ws.iterdir()), ["AGENTS.md", "answers", "docs", "progress.md", "sources", "tasks"])
        q = self.items()
        self.assertEqual(sorted(q), ["GUI-103__decide", "GUI-104__check_langs", "GUI-105__write_doc", "GUI-106__write_doc",
                                     "NOTASK_0.10.0_7__decide", "release_notes"])
        self.assertTrue(all(i["status"] == "prepared" for i in q.values()))
        names = {p.relative_to(self.ws).as_posix() for p in self.ws.rglob("*") if p.is_file()}
        self.assertFalse(any("TODO" in n or "RULES" in n or n.endswith(".py") or "reference" in n for n in names))
        # источники только своих задач; отброшенной GUI-101 и чужой point3-задачи GUI-102 нет
        self.assertEqual(sorted(p.name for p in (self.ws / "sources").iterdir()),
                         ["GUI-103", "GUI-104", "GUI-105", "GUI-106", "NOTASK_0.10.0_7"])
        task = (self.ws / "tasks" / "GUI-105__write_doc.md").read_text(encoding="utf-8")
        self.assertIn("- `docs/MD_rus/docs/user-manual/100_Orders.md`", task)
        self.assertIn("- `sources/GUI-105/changes.md` - сводка всех изменений кода по задаче", task)
        self.assertIn("строка 1: Раздел \"Заказы\"", task)                    # заголовки страницы с номерами строк
        digest = (self.ws / "sources" / "GUI-105" / "changes.md").read_text(encoding="utf-8")
        self.assertIn("+export const ruRu = { status: 'Статус', reset: 'Сбросить', date: 'Дата заказа'", digest)
        self.assertNotIn("orders.spec.js", digest)                              # тесты в сводку не идут
        self.assertFalse((self.ws / "sources" / "GUI-105" / "code").exists())   # полные файлы кода не копируются
        self.assertTrue((self.ws / "sources" / "GUI-105" / "patches").is_dir())
        self.assertNotIn("GUI-106", task)
        self.assertNotIn("{{ITEM}}", task)
        rules = (self.ws / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("1. `tasks/GUI-103__decide.md`", rules)
        for kind in ("decide", "write_doc", "check_doc", "check_langs", "release_notes"):
            self.assertIn(f"## Вид задания `{kind}`", rules)
        self.assertNotIn("{{", rules.replace("{{PRODUCT_NAME}}", ""))
        # общие правила лежат один раз в AGENTS.md, в задании только данные задачи
        rn_task = (self.ws / "tasks" / "release_notes.md").read_text(encoding="utf-8")
        self.assertIn("### GUI-105 (изменение)", rn_task)
        self.assertIn("- запись разработчиков: Добавлен фильтр заказов по дате", rn_task)
        self.assertIn("### GUI-103 (решение принимаешь ты в задании decide", rn_task)
        self.assertNotIn("GUI-101", rn_task)                      # отброшенные задачи в задание не попадают
        self.assertIn("Вид задания: `write_doc`", task)
        self.assertNotIn("Правила работы с источниками", task)
        self.assertLess(len(task), len(rules))
        self.assertIn("Агент запускается вручную", self.summary())

    def test_agent_gets_only_what_rules_could_not_do(self):
        self.approve(**{"NOTASK:0.10.0#7": 1})     # человек сам отклонил задачу с низкой достоверностью
        run("agent", self.out)
        self.assertEqual(sorted(i["kind"] for i in self.items().values()), ["check_langs", "release_notes", "write_doc", "write_doc"])

    def test_run_with_stub(self):
        self.approve(**{"GUI-103": 2})
        run("agent", self.out, "--cmd", STUB)
        q = self.items()
        self.assertTrue(all(i["status"] == "done" for i in q.values()), q)
        self.assertEqual(q["GUI-103__decide"]["decision"], "rn")
        self.assertEqual(q["GUI-105__write_doc"]["log"], ["changelog: прочитал записи задачи"])
        final = json.loads((self.out / P.OUT_AGENT / "final_decisions.json").read_text(encoding="utf-8"))
        self.assertEqual((final["GUI-103"], final["NOTASK:0.10.0#7"]), ("rn", "skip"))
        # правку внес код, а не агент: в result/ она есть, запись в trace.md сделана кодом
        page = (self.out / P.OUT_RESULT / self.page).read_text(encoding="utf-8")
        self.assertIn("Правка заглушки GUI-105__write_doc.", page)
        self.assertEqual(q["GUI-105__write_doc"]["applied"], [{"file": "docs/MD_rus/docs/user-manual/100_Orders.md", "what": "добавлена строка"}])
        self.assertIn("| GUI-105 | rus | user-manual/100_Orders.md | добавлена строка | агент, проверено кодом |",
                      (self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8"))
        # release notes собрал код из формулировок агента: структура как в черновике, комментариев нет
        rn = (self.out / P.OUT_RESULT / "MD_rus" / "docs" / "release-notes" / "020.md").read_text(encoding="utf-8")
        self.assertNotIn("<!--", rn)
        self.assertIn("# [0.2.0] 01.03.2026 {-}", rn)
        self.assertRegex(rn, r"\| point-ui\s+\| 0\.10\.0\s+\|")
        self.assertIn('- **[Раздел "Заказы"]**: Изменение по задаче GUI-105.', rn)
        self.assertIn("**Исправленные ошибки**", rn)
        self.assertIn("Изменение по задаче GUI-103.", rn)
        self.assertNotIn("NOTASK", rn)
        en = (self.out / P.OUT_RESULT / "MD_eng" / "docs" / "release-notes" / "020.md").read_text(encoding="utf-8")
        self.assertIn('- **["Orders" section]**: Change for task GUI-105.', en)
        s = self.summary()
        self.assertIn("Состояние заданий: принято - 6.", s)
        self.assertIn("Внесено кодом:", s)
        self.assertIn("- GUI-103: строка в release notes", s)
        self.assertNotIn("Агент запускается вручную", s)

    def test_progress_and_timing_are_in_the_journal(self):
        self.approve()
        run("agent", self.out, "--cmd", STUB)
        progress = (self.out / P.OUT_AGENT / "progress.md").read_text(encoding="utf-8")
        self.assertIn("НАЧАЛ GUI-105__write_doc", progress)
        self.assertIn("ГОТОВО release_notes", progress)
        self.assertTrue(all(isinstance(i["seconds"], int) for i in self.items().values()))
        self.assertIn("| Задание | Состояние | Решение | Правок внесено | Время | Итог |", self.summary())

    def test_two_tasks_can_edit_the_same_page(self):
        # GUI-104 и GUI-106 правят одну страницу 050_Work.md: правки вносятся по очереди, обе приняты
        self.approve(**{"NOTASK:0.10.0#7": 1})
        run("agent", self.out)
        task = (self.ws / "tasks" / "GUI-106__write_doc.md").read_text(encoding="utf-8")
        self.assertIn("docs/MD_rus/docs/user-manual/050_Work.md", task)
        answers = self.ws / "answers"
        code = re.search(r"`(R-[0-9a-f]{8})`", (self.ws / "AGENTS.md").read_text(encoding="utf-8")).group(1)
        page = "docs/MD_rus/docs/user-manual/050_Work.md"

        def answer(item, find, replace, quote):
            ev = [{"source": "changelog", "quote": quote}]
            (answers / f"{item}.json").write_text(json.dumps({
                "item": item, "rules": code, "status": "done", "summary": "правка",
                "steps": [{"source": "changelog", "found": "запись"}],
                "edits": [{"file": page, "find": find, "replace": replace, "what": f"правка {item}", "evidence": ev}]},
                ensure_ascii=False), encoding="utf-8")

        answer("GUI-106__write_doc", "# Работа с системой", "# Работа с системой\n\nПорядок пунктов меню изменен.",
               "Исправлен порядок пунктов меню")
        answer("GUI-104__check_langs", "русский, английский.", "русский, английский, испанский, португальский.",
               "добавлены языки es и pt")
        run("agent", self.out)                   # остальные задания ждут агента: это не ошибка
        q = self.items()
        self.assertEqual((q["GUI-106__write_doc"]["status"], q["GUI-104__check_langs"]["status"]), ("done", "done"))
        text = (self.out / P.OUT_RESULT / "MD_rus" / "docs" / "user-manual" / "050_Work.md").read_text(encoding="utf-8")
        self.assertIn("Порядок пунктов меню изменен.", text)
        self.assertIn("русский, английский, испанский, португальский.", text)

    def test_agent_asks_for_second_round(self):
        self.approve(**{"NOTASK:0.10.0#7": 1})
        run("agent", self.out, "--cmd", f"{STUB} recheck")          # не ошибка: правки внесены, но помечены
        q = self.items()
        self.assertEqual(q["GUI-105__write_doc"]["status"], "recheck")
        self.assertEqual(q["GUI-105__write_doc"]["recheck_reason"], "изменение можно понять двумя способами")
        self.assertIn("**Агент просит второй круг**", self.summary())
        self.assertFalse(any("round2" in k for k in q))
        run("agent", self.out)                                      # после проверки человеком готовится второй круг
        q = self.items()
        self.assertEqual(q["GUI-105__write_doc__round2"]["status"], "prepared")
        task = (self.ws / "tasks" / "GUI-105__write_doc__round2.md").read_text(encoding="utf-8")
        self.assertIn("## Второй круг", task)
        self.assertIn("агент: изменение можно понять двумя способами", task)
        self.assertIn('"recheck_reason"', task)                     # первый ответ приложен

    def test_human_sends_task_to_second_round(self):
        self.approve(**{"NOTASK:0.10.0#7": 1})
        run("agent", self.out, "--cmd", STUB)
        run("agent", self.out, "--recheck", "GUI-106", "--note", "порядок пунктов описан неверно")
        q = self.items()
        self.assertIn("GUI-106__write_doc__round2", q)
        self.assertNotIn("GUI-105__write_doc__round2", q)
        task = (self.ws / "tasks" / "GUI-106__write_doc__round2.md").read_text(encoding="utf-8")
        self.assertIn("человек: порядок пунктов описан неверно", task)

    def test_second_run_does_not_call_agent_again_and_does_not_apply_twice(self):
        self.approve()
        run("agent", self.out, "--cmd", STUB)
        run("agent", self.out, "--cmd", STUB + " fail")
        self.assertTrue(all(i["status"] == "done" for i in self.items().values()))
        page = (self.out / P.OUT_RESULT / self.page).read_text(encoding="utf-8")
        self.assertEqual(page.count("Правка заглушки GUI-105__write_doc."), 1)
        self.assertEqual((self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8").count("| GUI-105 | rus |"), 1)

    def test_unverified_answers_are_not_applied(self):
        self.approve(**{"NOTASK:0.10.0#7": 1})
        cases = (("no_evidence", "нет подтверждения"), ("fake_quote", "цитаты нет в источнике changelog"),
                 ("foreign_source", "источник не входит в разрешенные"), ("no_steps", "нет списка steps"),
                 ("foreign_step", "открыт файл вне источников задачи"),
                 ("fake_term", "названия «Несуществующая кнопка» нет"), ("big_edit", "правка слишком большая"),
                 ("bad_find", "фрагмента find нет на странице"), ("wrong_page", "страница не входит в страницы задания"),
                 ("comment", "в новом тексте комментарий"), ("no_rules", "не прочитал"))
        for mode, msg in cases:
            with self.assertRaises(SystemExit) as cm:
                run("agent", self.out, "--cmd", f"{STUB} {mode}", "--redo")
            self.assertEqual(cm.exception.code, 1)
            item = self.items()["GUI-105__write_doc"]
            self.assertEqual(item["status"], "unverified", mode)
            self.assertTrue(any(msg in p for p in item["problems"]) or msg in item["note"], (mode, item["problems"]))
            self.assertEqual(item["applied"], [])
            self.assertNotIn("Правка заглушки", (self.out / P.OUT_RESULT / self.page).read_text(encoding="utf-8"), mode)
            self.assertNotIn("GUI-105", (self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8"), mode)
        s = self.summary()
        self.assertIn("**Нужно переделать**", s)
        self.assertIn("Что не подтверждено:", s)

    def test_direct_edit_of_docs_is_ignored_and_reported(self):
        self.approve(**{"NOTASK:0.10.0#7": 1})
        run("agent", self.out, "--cmd", f"{STUB} direct")
        run("agent", self.out)
        self.assertEqual(self.queue()["touched"], [])                 # после первого запуска папка возвращена
        self.assertNotIn("Прямая правка", (self.out / P.OUT_RESULT / "MD_rus" / "docs" / "index.md").read_text(encoding="utf-8"))
        self.assertNotIn("Прямая правка", (self.ws / "docs" / "MD_rus" / "docs" / "index.md").read_text(encoding="utf-8"))
        # правка из ответа при этом принята
        self.assertIn("Правка заглушки", (self.out / P.OUT_RESULT / self.page).read_text(encoding="utf-8"))

    def test_release_notes_wording_is_checked(self):
        self.approve(**{"NOTASK:0.10.0#7": 1})
        for mode, msg in (("rn_extra", "задач, которые в release notes не идут: GUI-999"), ("rn_code", "имя из кода")):
            with self.assertRaises(SystemExit):
                run("agent", self.out, "--cmd", f"{STUB} {mode}", "--redo")
            item = self.items()["release_notes"]
            self.assertEqual(item["status"], "unverified", mode)
            self.assertTrue(any(msg in p for p in item["problems"]), (mode, item["problems"]))
            rn = (self.out / P.OUT_RESULT / "MD_rus" / "docs" / "release-notes" / "020.md").read_text(encoding="utf-8")
            self.assertIn(DRAFT.DRAFT_MARK, rn)                       # черновик остался черновиком

    def test_unverified_decision_is_not_applied(self):
        self.approve(**{"GUI-103": 2})
        with self.assertRaises(SystemExit):
            run("agent", self.out, "--cmd", f"{STUB} fake_quote")
        self.assertEqual(self.items()["GUI-103__decide"]["status"], "unverified")
        final = json.loads((self.out / P.OUT_AGENT / "final_decisions.json").read_text(encoding="utf-8"))
        self.assertEqual(final["GUI-103"], "agent")
        self.assertEqual(self.items()["release_notes"]["status"], "unverified")   # состав строк еще не определен

    def test_agent_failures_are_reported(self):
        self.approve(**{"NOTASK:0.10.0#7": 1})
        for mode, msg in (("bad_json", "не является JSON"), ("fail", "кодом 3")):
            with self.assertRaises(SystemExit):
                run("agent", self.out, "--cmd", f"{STUB} {mode}", "--redo")
            self.assertTrue(any(msg in i["note"] for i in self.items().values()), mode)

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
        rules = (ROOT / "agent" / "prompts" / "workspace_agents.md").read_text(encoding="utf-8")
        for kind in AG.KIND_ORDER:
            self.assertIn(f"## Вид задания `{kind}`", rules)
        self.assertTrue((ROOT / "agent" / "prompts" / "task.md").is_file())

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
        self.assertFalse((self.out / P.OUT_WORKSPACE).exists())

    def test_runs_when_reference_is_outside_then_compare_by_path(self):
        shutil.move(str(self.data / P.SLOT_DOCS_REFERENCE), str(self.ref))
        CLI.main(["collect", str(self.data), "--out", str(self.out)])
        self.agent()
        self.assertTrue((self.out / P.OUT_WORKSPACE / "tasks").is_dir())
        CLI.main(["compare", str(self.data), "--out", str(self.out), "--reference", str(self.ref)])
        self.assertTrue((self.out / P.OUT_COMPARE / "compare.html").is_file())
        with self.assertRaises(SystemExit):     # после сравнения результаты лежат в output: агент снова не запускается
            self.agent("--redo")

    def test_preparing_tasks_is_always_allowed(self):
        CLI.main(["collect", str(self.data), "--out", str(self.out)])
        CLI.main(["agent", str(self.data), "--out", str(self.out), "--approve"])
        self.assertTrue((self.out / P.OUT_WORKSPACE / "tasks").is_dir())


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
    """Агент запускается вручную (чат в редакторе): пайплайн готовит папку и потом проверяет готовые ответы."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, True)
        run("collect", self.out)
        run("agent", self.out, "--approve")                       # агент не настроен: только задания
        self.answers = self.out / P.OUT_WORKSPACE / "answers"
        self.code = re.search(r"`(R-[0-9a-f]{8})`", (self.out / P.OUT_WORKSPACE / "AGENTS.md").read_text(encoding="utf-8")).group(1)

    def answer(self, evidence):
        (self.answers / "NOTASK_0.10.0_7__decide.json").write_text(json.dumps(
            {"item": "NOTASK_0.10.0_7__decide", "rules": self.code, "status": "done", "summary": "сборка, пользователю не видно",
             "decision": "skip", "steps": [{"source": "changelog", "found": "запись про сборку"}], "evidence": evidence},
            ensure_ascii=False), encoding="utf-8")

    def state(self):
        q = json.loads((self.out / P.OUT_AGENT / "queue.json").read_text(encoding="utf-8"))
        final = json.loads((self.out / P.OUT_AGENT / "final_decisions.json").read_text(encoding="utf-8"))
        return {i["id"]: i for i in q["items"]}, final

    def test_verified_answer_is_picked_up(self):
        self.answer([{"source": "changelog", "quote": "Обновлены зависимости сборки"}])
        run("agent", self.out, "--approve")
        q, final = self.state()
        self.assertEqual(q["NOTASK_0.10.0_7__decide"]["status"], "done")
        self.assertEqual(q["GUI-105__write_doc"]["status"], "prepared")
        self.assertEqual(final["NOTASK:0.10.0#7"], "skip")

    def test_quote_copied_with_the_line_prefix_is_accepted(self):
        # агент часто копирует запись вместе с пометками из задания: это та же запись
        self.answer([{"source": "changelog", "quote": "[Изменено] [build] Обновлены зависимости сборки"}])
        run("agent", self.out, "--approve")
        self.assertEqual(self.state()[0]["NOTASK_0.10.0_7__decide"]["status"], "done")

    def test_invented_explanation_is_not_accepted(self):
        self.answer([{"source": "changelog", "quote": "меню изменено и добавлены английские названия"}])
        with self.assertRaises(SystemExit):
            run("agent", self.out, "--approve")
        q, final = self.state()
        self.assertEqual(q["NOTASK_0.10.0_7__decide"]["status"], "unverified")
        self.assertEqual(final["NOTASK:0.10.0#7"], "agent")


if __name__ == "__main__":
    unittest.main()
