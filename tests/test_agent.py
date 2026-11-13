"""Агент: папка и задания, проверка ответов, внесение правок, повторная попытка, release notes."""

import json
import re
import shutil
import unittest
from pathlib import Path

from pipeline import product as P
from pipeline import validation as V
from tests.helpers import STUB, Workdir, mark_review

PAGE = "docs/MD_rus/docs/user-manual/100_Orders.md"
FIND = "Фильтр «Статус» отбирает заказы по статусу."


class AgentCase(Workdir):
    def setUp(self):
        super().setUp()
        shutil.rmtree(self.data / P.SLOT_DOCS_REFERENCE)           # агент не должен видеть документацию техписа
        cfg = json.loads((self.data / "dataset.json").read_text(encoding="utf-8"))
        cfg.update(release="0.2.0", release_date="01.03.2026")
        (self.data / "dataset.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.cli("collect")
        self.ws = self.out / P.OUT_WORKSPACE
        self.rn = self.out / P.OUT_RESULT / "MD_rus/docs/release-notes/020.md"

    def start(self, *extra, **marks):
        mark_review(self.out, **marks)
        self.cli("agent", *extra)

    def items(self):
        q = json.loads((self.out / P.OUT_AGENT / "queue.json").read_text(encoding="utf-8"))
        return {i["id"]: i for i in q["items"]}

    def summary(self):
        return (self.out / P.OUT_AGENT / "summary.md").read_text(encoding="utf-8")

    def result(self, rel="MD_rus/docs/user-manual/100_Orders.md"):
        return (self.out / P.OUT_RESULT / rel).read_text(encoding="utf-8")

    def entry(self):
        task = (self.ws / "tasks/GUI-105__write_doc.md").read_text(encoding="utf-8")
        return re.search(r"^- \[значимая\] \[.*?\] (.+)$", task, flags=re.M).group(1)

    def answer(self, item, **fields):
        data = {"status": "done", "summary": "ок", "steps": [{"source": "changelog", "found": "запись"}]}
        data.update(fields)
        (self.ws / "answers").mkdir(parents=True, exist_ok=True)
        (self.ws / "answers" / f"{item}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def edit(self, quote=None, replace=None, find=FIND, page=PAGE):
        return {"edits": [{"file": page, "find": find, "replace": replace or FIND[:-1] + " и по дате.", "what": "уточнение",
                           "evidence": [{"source": "changelog", "quote": quote or self.entry()}]}]}


class WorkspaceTest(AgentCase):
    def test_refuses_until_reviewed(self):
        with self.assertRaises(SystemExit):
            self.cli("agent")
        self.assertFalse(self.ws.exists())

    def test_workspace_holds_only_what_tasks_need(self):
        self.start(**{"GUI-103": 2})
        self.assertEqual(sorted(p.name for p in self.ws.iterdir()), ["AGENTS.md", "answers", "docs", "progress.md", "sources", "tasks"])
        self.assertEqual(sorted(self.items()), ["GUI-103__decide", "GUI-104__write_doc", "GUI-105__write_doc", "GUI-106__write_doc",
                                                "NOTASK_0.10.0_7__decide", "release_notes"])
        names = {p.relative_to(self.ws).as_posix() for p in self.ws.rglob("*") if p.is_file()}
        self.assertFalse(any(x in n for n in names for x in ("TODO", "RULES", ".py", "reference")))
        self.assertEqual(sorted(p.name for p in (self.ws / "sources").iterdir()),
                         ["GUI-103", "GUI-104", "GUI-105", "GUI-106", "NOTASK_0.10.0_7"])     # отброшенных задач нет
        task = (self.ws / "tasks/GUI-105__write_doc.md").read_text(encoding="utf-8")
        self.assertIn("- `docs/MD_rus/docs/user-manual/100_Orders.md`", task)
        self.assertIn("- `sources/GUI-105/changes.md`", task)
        self.assertNotIn("GUI-106", task)
        self.assertIn("{{", (self.ws / "AGENTS.md").read_text(encoding="utf-8").replace("{{PRODUCT_NAME}}", "") and "{{")
        digest = (self.ws / "sources/GUI-105/changes.md").read_text(encoding="utf-8")
        self.assertIn("OrdersFilter.tsx [source, modified, используется приложением POINT 2", digest)
        self.assertNotIn("orders.spec.js", digest)                       # тесты в сводку не идут
        self.assertFalse((self.ws / "sources/GUI-105/code").exists())    # полные файлы кода не копируются

    def test_human_rejection_removes_the_task(self):
        self.start(**{"NOTASK:0.10.0#7": 1})
        self.assertEqual(sorted(i["kind"] for i in self.items().values()), ["release_notes", "write_doc", "write_doc", "write_doc"])

    def test_language_task_gets_app_settings(self):
        self.start()
        self.assertTrue((self.ws / "sources/GUI-104/changes.md").is_file())


class FullRunTest(AgentCase):
    def test_everything_is_done_and_applied_by_code(self):
        self.start("--cmd", STUB, **{"GUI-103": 2})
        q = self.items()
        self.assertTrue(all(i["status"] == "done" for i in q.values()), {k: v["status"] for k, v in q.items()})
        self.assertEqual(q["GUI-103__decide"]["decision"], "rn")
        self.assertIn("Правка заглушки GUI-105__write_doc.", self.result())
        self.assertIn("| GUI-105 | rus | user-manual/100_Orders.md | добавлена строка | агент, проверено кодом |",
                      (self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8"))
        rn = self.rn.read_text(encoding="utf-8")
        self.assertIn("# [0.2.0] 01.03.2026 {-}", rn)
        self.assertRegex(rn, r"\| point-ui\s+\| 0\.10\.0\s+\|")
        changes, fixes = rn.split("**Исправленные ошибки**")
        self.assertIn("- **[Общее]**: Изменение по задаче GUI-105.", changes)
        self.assertTrue("GUI-106" in fixes and "GUI-103" in fixes and "NOTASK" not in rn)      # раздел по типу записей
        self.assertIn('- **[General]**: Change for task GUI-105.',
                      (self.out / P.OUT_RESULT / "MD_eng/docs/release-notes/020.md").read_text(encoding="utf-8"))
        self.assertIn("- 020.md\n        - 010.md", (self.out / P.OUT_RESULT / "MD_rus/docs/release-notes/.pages").read_text(encoding="utf-8"))
        self.assertIn("Состояние: принято - 7.", self.summary())
        self.assertIn("- GUI-103: строка в release notes", self.summary())
        self.assertIn("ГОТОВО GUI-105__write_doc", (self.out / P.OUT_AGENT / "progress.md").read_text(encoding="utf-8"))
        self.assertTrue(all(isinstance(i["seconds"], int) for i in q.values()))

    def test_rerun_neither_calls_the_agent_nor_applies_twice(self):
        self.start("--cmd", STUB)
        self.cli("agent", "--cmd", STUB + " fail")
        self.assertTrue(all(i["status"] == "done" for i in self.items().values()))
        self.assertEqual(self.result().count("Правка заглушки GUI-105__write_doc."), 1)
        self.assertEqual((self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8").count("| GUI-105 | rus |"), 1)

    def test_two_tasks_edit_the_same_page(self):
        self.start()                                       # GUI-104 и GUI-106 правят 050_Work.md
        page = "docs/MD_rus/docs/user-manual/050_Work.md"
        self.answer("GUI-106__write_doc", **self.edit(quote="Исправлен порядок пунктов меню", page=page, find="# Работа с системой",
                                                      replace="# Работа с системой\n\nПорядок пунктов меню изменен."))
        self.answer("GUI-104__write_doc", **self.edit(quote="добавлены языки es и pt", page=page, find="русский, английский.",
                                                      replace="русский, английский, испанский, португальский."))
        self.cli("agent")
        text = self.result("MD_rus/docs/user-manual/050_Work.md")
        self.assertIn("Порядок пунктов меню изменен.", text)
        self.assertIn("русский, английский, испанский, португальский.", text)

    def test_screenshot_list_is_built_by_code_after_edits(self):
        self.start("--cmd", STUB)
        shots = (self.out / P.OUT_RESULT / "screenshots.md").read_text(encoding="utf-8")
        self.assertIn("| rus | `user-manual/100_Orders.md`:", shots)
        self.assertNotIn("SCREENSHOT-TODO", self.result())


class RejectionTest(AgentCase):
    """Ответ, не прошедший проверку, не вносится. Одна повторная попытка, затем отказ."""

    BAD = (("no_evidence", "нет подтверждения"), ("fake_quote", "цитаты нет в источнике changelog"), ("fake_term", "названия «Несуществующая кнопка» нет"),
           ("big_edit", "правка слишком большая"), ("bad_find", "фрагмента find нет на странице"),
           ("wrong_page", "страница не входит в страницы задания"), ("comment", "в новом тексте комментарий"),
           ("shot", "метки скриншотов не ставятся"), ("bad_json", "не является JSON"), ("fail", "кодом 3"))

    def test_bad_answers_are_dropped_after_two_attempts(self):
        mark_review(self.out, **{"NOTASK:0.10.0#7": 1})
        for mode, message in self.BAD:
            with self.subTest(mode):
                self.cli("agent", "--cmd", f"{STUB} {mode}", "--redo")
                q = self.items()
                first, retry = q["GUI-105__write_doc"], q["GUI-105__write_doc__retry"]
                self.assertEqual((first["status"], retry["status"]), ("rejected", "dropped"))
                self.assertTrue(any(message in p for p in first["problems"]), first["problems"])
                self.assertNotIn("Правка заглушки", self.result())            # ничего не внесено
                self.assertNotIn("GUI-105", (self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8"))
        self.assertIn("не внесено: отклонено дважды", self.summary())

    def test_steps_are_only_notes(self):
        mark_review(self.out, **{"NOTASK:0.10.0#7": 1})
        for mode, note in (("no_steps", "нет списка steps"), ("foreign_step", "вне источников задачи")):
            with self.subTest(mode):
                self.cli("agent", "--cmd", f"{STUB} {mode}", "--redo")
                q = self.items()["GUI-105__write_doc"]
                self.assertEqual((q["status"], len(q["applied"])), ("done", 1))
                self.assertTrue(any(note in n for n in q["notes"]), q["notes"])

    def test_one_bad_edit_does_not_cancel_the_good_one(self):
        self.start(**{"NOTASK:0.10.0#7": 1})
        good = self.edit()
        bad = self.edit()["edits"][0]
        bad["find"], bad["what"] = "такой строки на странице нет", "плохая правка"
        good["edits"].append(bad)
        self.answer("GUI-105__write_doc", **good)
        self.cli("agent")
        q = self.items()["GUI-105__write_doc"]
        self.assertEqual((q["status"], len(q["applied"])), ("partial", 1))
        self.assertTrue(any("правка 2" in p for p in q["problems"]), q["problems"])
        self.assertIn("и по дате.", self.result())
        task = (self.ws / "tasks/GUI-105__write_doc__retry.md").read_text(encoding="utf-8")
        self.assertIn("правка 2", task)

    def test_new_variable_and_missing_link_are_rejected(self):
        self.start(**{"NOTASK:0.10.0#7": 1})
        for text, message in (("Продукт {{ NO_SUCH_VARIABLE }}.", "переменной"), ("См. [работу](./no_such_page.md).", "несуществующую страницу")):
            with self.subTest(message):
                self.answer("GUI-105__write_doc", **self.edit(replace=FIND + "\n\n" + text))
                self.cli("agent")
                q = self.items()["GUI-105__write_doc"]
                self.assertTrue(any(message in p for p in q["problems"]), q["problems"])
        self.answer("GUI-105__write_doc", **self.edit(replace=FIND + "\n\nСм. [работу](./050_Work.md)."))
        self.cli("agent")
        self.assertEqual(self.items()["GUI-105__write_doc"]["status"], "done")

    def test_second_attempt_can_succeed(self):
        self.start("--cmd", f"{STUB} bad_then_ok", **{"NOTASK:0.10.0#7": 1})
        q = self.items()
        self.assertEqual((q["GUI-105__write_doc"]["status"], q["GUI-105__write_doc__retry"]["status"]), ("rejected", "done"))
        self.assertIn("Правка заглушки GUI-105__write_doc__retry.", self.result())
        task = (self.ws / "tasks/GUI-105__write_doc__retry.md").read_text(encoding="utf-8")
        self.assertIn("## Повторная попытка", task)
        self.assertIn("цитаты нет в источнике changelog", task)

    def test_manual_flow_prepares_retry_after_a_bad_answer(self):
        self.start(**{"NOTASK:0.10.0#7": 1})
        self.answer("GUI-105__write_doc", **self.edit(quote="такой записи нет в списке изменений совсем"))
        self.cli("agent")
        self.assertEqual((self.items()["GUI-105__write_doc"]["status"], self.items()["GUI-105__write_doc__retry"]["status"]),
                         ("rejected", "prepared"))
        self.assertTrue((self.ws / "tasks/GUI-105__write_doc__retry.md").is_file())
        self.answer("GUI-105__write_doc__retry", **self.edit())
        self.cli("agent")
        self.assertEqual(self.items()["GUI-105__write_doc__retry"]["status"], "done")
        self.assertIn("и по дате.", self.result())

    def test_unknown_is_final_and_applies_nothing(self):
        self.start("--cmd", f"{STUB} unknown", **{"NOTASK:0.10.0#7": 1})
        q = self.items()
        self.assertEqual(q["GUI-105__write_doc"]["status"], "unknown")
        self.assertNotIn("GUI-105__write_doc__retry", q)
        self.assertNotIn("Правка заглушки", self.result())

    def test_direct_edit_of_docs_is_ignored(self):
        self.start("--cmd", f"{STUB} direct", **{"NOTASK:0.10.0#7": 1})
        self.assertNotIn("Прямая правка", self.result("MD_rus/docs/index.md"))
        self.assertNotIn("Прямая правка", (self.ws / "docs/MD_rus/docs/index.md").read_text(encoding="utf-8"))
        self.assertIn("Правка заглушки", self.result())                 # правка из ответа внесена

    def test_verified_edits_are_applied_even_if_agent_is_unsure_about_the_rest(self):
        self.start(**{"NOTASK:0.10.0#7": 1})
        self.answer("GUI-105__write_doc", status="unknown", questions=["как называется кнопка?"], **self.edit())
        self.cli("agent")
        q = self.items()["GUI-105__write_doc"]
        self.assertEqual((q["status"], len(q["applied"])), ("unknown", 1))
        self.assertIn("и по дате.", self.result())


class LaunchFailureTest(AgentCase):
    def test_failed_launch_is_repeated_before_it_counts_as_a_rejection(self):
        self.start("--cmd", f"{STUB} fail", **{"NOTASK:0.10.0#7": 1})
        self.assertTrue((self.out / P.OUT_AGENT / "logs" / "GUI-105__write_doc.log").is_file())
        self.assertIn("кодом 3", self.items()["GUI-105__write_doc"]["problems"][0])


class ChainTest(AgentCase):
    """Решение агента «править документацию» и находки проверок приводят к заданиям, а не теряются."""

    def test_decision_doc_creates_write_doc_task(self):
        self.start(**{"GUI-105": 2, "NOTASK:0.10.0#7": 1})
        item = "GUI-105"
        self.assertNotIn(f"{item}__write_doc", self.items())
        task = (self.ws / f"tasks/{item}__decide.md").read_text(encoding="utf-8")
        quote = re.search(r"^- \[[^\]]+\] \[[^\]]*\] \[.*?\] (.+)$", task, flags=re.M).group(1)[:60]
        self.answer(f"{item}__decide", decision="doc", evidence=[{"source": "changelog", "quote": quote}])
        self.cli("agent")
        q = self.items()
        self.assertEqual(q[f"{item}__decide"]["decision"], "doc")
        self.assertEqual(q[f"{item}__write_doc"]["status"], "prepared")
        self.assertTrue((self.ws / f"tasks/{item}__write_doc.md").is_file())

    def test_task_without_pages_is_not_given_to_the_agent(self):
        for lang in ("rus", "eng"):
            (self.out / P.OUT_RESULT / f"MD_{lang}/docs/user-manual/100_Orders.md").unlink()
        self.start("--cmd", STUB, **{"NOTASK:0.10.0#7": 1})
        q = self.items()["GUI-105__write_doc"]
        self.assertEqual(q["status"], "unknown")
        self.assertIn("не найдена страница", q["note"])

    def test_checks_after_edits_become_a_task_and_a_fix_is_applied(self):
        self.start("--cmd", STUB, **{"NOTASK:0.10.0#7": 1})
        task = (self.ws / "tasks/checks_fix.md").read_text(encoding="utf-8")
        self.assertIn("нет файла картинки", task)
        page = "docs/MD_eng/docs/user-manual/100_Orders.md"
        line = next(l for l in (self.ws / page).read_text(encoding="utf-8").splitlines() if "image001.png" in l)
        data = {"edits": [{"file": page, "find": line + "\n", "replace": "", "what": "убрана ссылка на картинку без файла",
                           "evidence": [{"source": page, "quote": line}]}]}
        self.answer("checks_fix", **data)
        self.cli("agent")
        q = self.items()["checks_fix"]
        self.assertEqual((q["status"], len(q["applied"])), ("done", 1), q["problems"])
        self.assertNotIn("image001.png", self.result("MD_eng/docs/user-manual/100_Orders.md"))
        self.cli("agent")                                                          # находок больше нет, запись сохраняется
        self.assertEqual(self.items()["checks_fix"]["status"], "done")


class QuoteTest(AgentCase):
    def test_real_quotes_are_accepted_and_invented_are_not(self):
        self.start(**{"NOTASK:0.10.0#7": 1})
        entry = self.entry()
        digest = (self.ws / "sources/GUI-105/changes.md").read_text(encoding="utf-8")
        head = next(l for l in digest.splitlines() if l.startswith("## "))
        hunk = next(l for l in digest.splitlines() if l.startswith("@@"))
        body = next(l for l in digest.splitlines() if l.startswith("+") and len(l) > 20)
        evidence = [{"source": "changelog", "quote": entry.lower()},                                    # другой регистр
                    {"source": "sources/GUI-105/changes.md", "quote": f"{head}\n{hunk}\n{body[1:]}"},   # через строку ```diff
                    {"source": "changelog", "quote": entry[:12] + "ы" + entry[12:]}]                    # опечатка
        data = self.edit()
        data["edits"][0]["evidence"] = evidence
        self.answer("GUI-105__write_doc", **data)
        self.cli("agent")
        q = self.items()["GUI-105__write_doc"]
        self.assertEqual(q["status"], "done", q["problems"])
        self.assertEqual(len(q["approx"]), 1)
        self.assertIn("Цитат принято с поправкой на опечатку: 1", self.summary())

    def test_invented_quote_is_rejected(self):
        self.start(**{"NOTASK:0.10.0#7": 1})
        self.answer("GUI-105__write_doc", **self.edit(quote="Добавлена возможность экспорта заказов в таблицу формата Excel"))
        self.cli("agent")
        self.assertEqual(self.items()["GUI-105__write_doc"]["status"], "rejected")


class ReleaseNotesTest(AgentCase):
    def test_waits_for_decisions_and_checks_lines(self):
        self.start()                                                   # у задачи NOTASK решение еще не принято
        self.assertEqual(self.items()["release_notes"]["status"], "waiting")
        self.assertFalse(self.rn.exists())

    def test_line_is_required_unless_a_reason_is_given(self):
        for mode, status, text in (("rn_missing", "partial", "строки нет"), ("rn_code", "partial", "имя из кода"),
                                   ("rn_no_line", "done", "без строки по решению агента")):
            with self.subTest(mode):
                self.cli("agent", "--cmd", f"{STUB} {mode}", "--redo") if self.ws.exists() else self.start("--cmd", f"{STUB} {mode}", **{"NOTASK:0.10.0#7": 1})
                rn = self.items().get("release_notes__retry") or self.items()["release_notes"]
                self.assertEqual(rn["status"], status)
                self.assertTrue(text in rn["note"] or any(text in p for p in rn["problems"]) or text in str(rn["applied"]), rn)

    def test_section_label_is_chosen_by_the_agent_and_translated_by_code(self):
        self.start(**{"NOTASK:0.10.0#7": 1})
        tasks = ["GUI-103", "GUI-104", "GUI-105", "GUI-106"]
        lines = [{"task": t, "section": 'Раздел "Заказы"' if t == "GUI-105" else "Общее", "ru": f"Изменение {t}.", "en": f"Change {t}."} for t in tasks]
        (self.ws / "answers").mkdir(parents=True, exist_ok=True)
        (self.ws / "answers/release_notes.json").write_text(json.dumps({"lines": lines}, ensure_ascii=False), encoding="utf-8")
        self.cli("agent")
        self.assertIn('- **[Раздел "Заказы"]**: Изменение GUI-105.', self.rn.read_text(encoding="utf-8"))
        self.assertIn('- **["Orders" section]**: Change GUI-105.',
                      (self.out / P.OUT_RESULT / "MD_eng/docs/release-notes/020.md").read_text(encoding="utf-8"))
        self.assertIn("- **[Общее]**: Изменение GUI-104.", self.rn.read_text(encoding="utf-8"))

    def test_file_follows_the_final_decisions(self):
        # агент решил по спорной задаче skip: ее строки в release notes нет
        self.start("--cmd", STUB)
        self.assertNotIn("NOTASK", self.rn.read_text(encoding="utf-8"))
        self.assertEqual(json.loads((self.out / P.OUT_AGENT / "final_decisions.json").read_text(encoding="utf-8"))["NOTASK:0.10.0#7"], "skip")


if __name__ == "__main__":
    unittest.main()
