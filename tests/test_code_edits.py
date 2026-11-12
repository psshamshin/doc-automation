"""Правки, которые код вносит сам: замены названий (Д4) и строки release notes из Jira."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import cli as CLI
from pipeline import draft as DRAFT
from pipeline import product as P
from pipeline import validation as V
from pipeline.layout import Dataset
from tests.helpers import DEMO, demo_units


class ReleaseNoteSourceTest(unittest.TestCase):
    def setUp(self):
        self.unit = dict(demo_units()["GUI-105"])

    def test_changelog_is_the_source(self):
        self.assertEqual(DRAFT.rn_source(self.unit),
                         {"source": "список изменений", "jira": "", "texts": ["Добавлен фильтр заказов по дате"]})

    def test_jira_title_is_extra_and_without_product_prefix(self):
        # название задачи в Jira внутреннее: в черновик идет запись разработчиков, название дается отдельно
        self.unit["jira"] = {"summary": "[point][ui] Доработка страницы Заказы"}
        src = DRAFT.rn_source(self.unit)
        self.assertEqual((src["jira"], src["texts"]), ("Доработка страницы Заказы", ["Добавлен фильтр заказов по дате"]))
        line = DRAFT.rn_block(self.unit, {}, "rus", False)[0]
        self.assertEqual(line, "- **[Общее]**: Добавлен фильтр заказов по дате <!-- GUI-105, источник: список изменений -->")


class JiraExportTest(unittest.TestCase):
    """Выгрузка Jira в формате 'Экспорт -> XML': метка продукта, строка release notes, задачи вне списка изменений."""

    XML = """<rss version="0.92"><channel>
<item><title>[GUI-103] [point] Заголовок страницы сбрасывается</title><key id="1">GUI-103</key>
  <summary>[point] Заголовок страницы сбрасывается при прокрутке</summary>
  <description>&lt;p&gt;Описание&lt;/p&gt;</description><type>Bug</type><status>Done</status><labels></labels></item>
<item><title>[GUI-105] [point3] Фильтр</title><key id="2">GUI-105</key><summary>[point3] Фильтр по дате</summary></item>
<item><title>[GUI-104] [shared] Языки</title><key id="4">GUI-104</key><summary>[shared] Набор языков</summary></item>
<item><title>[GUI-106] Меню</title><key id="5">GUI-106</key><summary>Порядок меню в Point 3</summary></item>
<item><title>[GUI-101] [point3] Селект</title><key id="6">GUI-101</key><summary>[point3] Перенос селекта</summary></item>
<item><title>[GUI-900] Без метки</title><key id="3">GUI-900</key><summary>Задача, которой нет в списке изменений</summary></item>
</channel></rss>"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        shutil.copytree(DEMO, self.data)
        (self.data / "SearchRequest.xml").write_text(self.XML, encoding="utf-8")
        self.out = self.tmp / "out"
        CLI.main(["collect", str(self.data), "--out", str(self.out)])
        self.units = {u["id"]: u for u in json.loads((self.out / P.OUT_DATA / "05_units.json").read_text(encoding="utf-8"))["units"]}
        self.review = (self.out / V.FILE_NAME).read_text(encoding="utf-8")

    def product(self, uid):
        d = self.units[uid]["decision"]["product"]
        return d["value"], d["rule"], d.get("jira_conflict")

    def test_point_prefix_decides(self):
        self.assertEqual(self.product("GUI-103"), ("point2", "В1", None))

    def test_other_product_prefix_does_not_override_code(self):
        # [point3] в названии, но код задачи использует POINT 2: задача остается, в файле проверки пометка И9
        self.assertEqual(self.product("GUI-105"), ("point2", "В5", "point3"))
        self.assertEqual(self.units["GUI-105"]["decision"]["action"]["value"], "doc_and_rn")
        self.assertIn("И9: Jira: point3", self.review)

    def test_other_product_named_in_title_text(self):
        self.assertEqual(self.product("GUI-106"), ("point2", "В3", "point3"))

    def test_tag_that_is_not_a_product_is_ignored(self):
        self.assertEqual(self.product("GUI-104"), ("point2", "В5", None))

    def test_other_product_prefix_without_point_code_drops_the_task(self):
        from pipeline import units as U
        from tests.helpers import RULES, entry
        e = entry("Добавлена панель отчета", ["monokit", "widgets/report-panel"], "add")
        unit = {"entries": [e]}
        p = "packages/monokit/widgets/report-panel/ReportPanel.tsx"
        got = U.decide_product(unit, {"product_tags": ["point3"], "summary": "[point3] Панель"}, {e["id"]: {p}}, set(), RULES)
        self.assertEqual((got["value"], got["rule"]), ("other", "В2"))

    def test_release_note_line_from_changelog(self):
        # в черновик идет запись разработчиков, а не внутреннее название задачи
        self.assertIn("| GUI-103 | Исправленные ошибки | Работа с системой | Исправлен сброс заголовка страницы при прокрутке. "
                      "Удалена лишняя обертка | список изменений |", self.review)

    def test_tasks_missing_from_changelog_are_listed(self):
        self.assertIn("## Задачи Jira, которых нет в списке изменений", self.review)
        self.assertIn("| GUI-900 | Задача, которой нет в списке изменений | - |", self.review)
        self.assertIn("| Задач Jira в выгрузке | 6 |", self.review)


class JiraQueryTest(unittest.TestCase):
    def test_no_control_query_without_dates(self):
        units = [{"task": "GUI-2"}, {"task": "ABC-1"}, {"task": None}]
        q = DRAFT.jira_queries(units, {"lower_date": None, "upper_date": "2026-03-01"})
        self.assertEqual(q, {"exact": "key in (ABC-1, GUI-2)", "control": ""})

    def test_projects_and_window(self):
        q = DRAFT.jira_queries([{"task": "GUI-2"}, {"task": "ABC-1"}], {"lower_date": "2026-01-10", "upper_date": "2026-03-01"})
        self.assertTrue(q["control"].startswith('project in (ABC, GUI) AND resolved >= "2026-01-10" AND resolved <= "2026-03-01 23:59"'))


class StaleNameTest(unittest.TestCase):
    """Текст исчез из интерфейса, а документация его упоминает: это находится, даже если запись задачи о другом."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        shutil.copytree(DEMO, self.data)
        i18n = self.data / P.SLOT_CODE_NEW / "packages/monokit/features/orders-filter/i18n.ts"
        i18n.write_text(i18n.read_text(encoding="utf-8").replace("reset: 'Сбросить', ", ""), encoding="utf-8")
        self.out = self.tmp / "out"
        CLI.main(["collect", str(self.data), "--out", str(self.out)])
        self.units = {u["id"]: u for u in json.loads((self.out / P.OUT_DATA / "05_units.json").read_text(encoding="utf-8"))["units"]}

    def test_removed_text_is_found_in_docs(self):
        u = self.units["GUI-105"]
        self.assertNotIn("Сбросить", " ".join(e["text"] for e in u["entries"]))     # в записи задачи текст не назван
        hits = u["doc_mentions"]["rus"]["Сбросить"]
        self.assertEqual([(h["file"], h["line"]) for h in hits], [("user-manual/050_Work.md", 5)])

    def test_agent_gets_the_page_and_the_removed_text(self):
        CLI.main(["agent", str(self.data), "--out", str(self.out), "--approve"])
        task = (self.out / P.OUT_WORKSPACE / "tasks" / "GUI-105__write_doc.md").read_text(encoding="utf-8")
        self.assertIn("- удален [ru]: «Сбросить»", task)
        self.assertIn("старый текст «Сбросить» в документации [rus]: `docs/MD_rus/docs/user-manual/050_Work.md`:5", task)
        self.assertIn("- `docs/MD_rus/docs/user-manual/050_Work.md`", task)

    def test_one_text_goes_to_one_task_only(self):
        owners = [uid for uid, u in self.units.items() if "Сбросить" in (u.get("doc_mentions") or {}).get("rus", {})]
        self.assertEqual(owners, ["GUI-105"])


class LanguagesFromKnowledgeTest(unittest.TestCase):
    def test_language_facts_reach_the_agent(self):
        from pipeline import agent as AG
        ds = Dataset(str(DEMO))
        ds.versions["languages_old"] = ["русский", "английский"]
        ds.versions["languages_new"] = ["русский", "английский", "испанский"]
        note = "\n".join(AG._languages_note(ds))
        self.assertIn("добавлены: испанский; убраны: нет", note)
        ds.versions["languages_new"] = None
        self.assertEqual(AG._languages_note(ds), [])


class RenameTest(unittest.TestCase):
    """Набор данных, в котором подпись 'Статус' переименована в 'Состояние заказа' и это названо в записи."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        shutil.copytree(DEMO, self.data)
        new = self.data / P.SLOT_CODE_NEW
        i18n = new / "packages/monokit/features/orders-filter/i18n.ts"
        i18n.write_text(i18n.read_text(encoding="utf-8").replace("status: 'Статус'", "status: 'Состояние заказа'"), encoding="utf-8")
        old = self.data / P.SLOT_CODE_OLD / "packages/monokit/features/orders-filter/i18n.ts"
        old.write_text(old.read_text(encoding="utf-8").replace("'Статус': 'Статус'", "status: 'Статус'"), encoding="utf-8")
        cl = new / P.APP_CHANGELOG
        cl.write_text(cl.read_text(encoding="utf-8").replace(
            "Добавлен фильтр заказов по дате", "Добавлен фильтр заказов по дате, фильтр «Статус» переименован в «Состояние заказа»"),
            encoding="utf-8")
        self.out = self.tmp / "out"
        CLI.main(["collect", str(self.data), "--out", str(self.out)])
        self.page = self.out / P.OUT_RESULT / "MD_rus" / "docs" / "user-manual" / "100_Orders.md"

    def test_planned_shown_but_not_applied_before_review(self):
        planned = json.loads((self.out / P.OUT_DATA / "07_review.json").read_text(encoding="utf-8"))["planned_renames"]
        self.assertEqual([(r["unit"], r["lang"], r["file"], r["old"], r["new"]) for r in planned],
                         [("GUI-105", "rus", "user-manual/100_Orders.md", "Статус", "Состояние заказа")])
        self.assertIn("| GUI-105 | rus | `user-manual/100_Orders.md`:7 | «Статус» | «Состояние заказа» |",
                      (self.out / V.FILE_NAME).read_text(encoding="utf-8"))
        self.assertIn("Фильтр «Статус» отбирает", self.page.read_text(encoding="utf-8"))

    def test_applied_after_review_for_accepted_task(self):
        CLI.main(["agent", str(self.data), "--out", str(self.out), "--approve"])
        self.assertIn("Фильтр «Состояние заказа» отбирает заказы по статусу.", self.page.read_text(encoding="utf-8"))
        trace = (self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8")
        self.assertEqual(trace.count("(код) «Статус» -> «Состояние заказа»"), 1)
        CLI.main(["agent", str(self.data), "--out", str(self.out), "--approve"])   # повтор ничего не меняет
        self.assertEqual((self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8"), trace)

    def test_not_applied_for_rejected_task(self):
        p = self.out / V.FILE_NAME
        lines = p.read_text(encoding="utf-8").replace("Проверено: [ ]", "Проверено: [x]").splitlines()
        lines = [l.replace("[x] | [ ] | [ ] |", "[ ] | [x] | [ ] |") if l.startswith("| GUI-105 ") else l for l in lines]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        CLI.main(["agent", str(self.data), "--out", str(self.out)])
        self.assertIn("Фильтр «Статус» отбирает", self.page.read_text(encoding="utf-8"))

    def test_string_not_named_in_entry_is_not_renamed(self):
        ds = Dataset(str(self.data), out=str(self.out))
        units = json.loads((self.out / P.OUT_DATA / "05_units.json").read_text(encoding="utf-8"))["units"]
        for u in units:
            for e in u["entries"]:
                e["text"] = e["text"].replace("«Статус»", "подпись").replace("«Состояние заказа»", "другую")
        self.assertEqual(DRAFT.planned_renames(ds, units), [])


if __name__ == "__main__":
    unittest.main()
