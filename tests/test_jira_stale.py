"""Выгрузка Jira (продукт, запросы, задачи вне списка) и поиск устаревших названий по всему релизу."""

import json
import re
import unittest

from pipeline import draft as DRAFT
from pipeline import product as P
from pipeline import validation as V
from tests.helpers import STUB, Workdir, demo_units, mark_review


class JiraTest(Workdir):
    XML = """<rss version="0.92"><channel>
<item><title>[GUI-103] [point] Заголовок</title><key id="1">GUI-103</key><summary>[point] Заголовок сбрасывается</summary>
  <description>&lt;p&gt;Описание&lt;/p&gt;</description><type>Bug</type><status>Done</status><labels></labels></item>
<item><title>[GUI-105] [point3] Фильтр</title><key id="2">GUI-105</key><summary>[point3] Фильтр по дате</summary></item>
<item><title>[GUI-104] [shared] Языки</title><key id="4">GUI-104</key><summary>[shared] Набор языков</summary></item>
<item><title>[GUI-106] Меню</title><key id="5">GUI-106</key><summary>Порядок меню в Point 3</summary></item>
<item><title>[GUI-101] [point3] Селект</title><key id="6">GUI-101</key><summary>[point3] Перенос селекта</summary></item>
<item><title>[GUI-900] Без метки</title><key id="3">GUI-900</key><summary>Задача, которой нет в списке изменений</summary></item>
</channel></rss>"""

    def setUp(self):
        super().setUp()
        (self.data / "SearchRequest.xml").write_text(self.XML, encoding="utf-8")
        self.cli("collect")
        self.units = {u["id"]: u for u in json.loads((self.out / P.OUT_DATA / "05_units.json").read_text(encoding="utf-8"))["units"]}
        self.review = (self.out / V.FILE_NAME).read_text(encoding="utf-8")

    def product(self, uid):
        d = self.units[uid]["decision"]["product"]
        return d["value"], d["rule"], d.get("jira_conflict")

    def test_point_prefix_decides(self):
        self.assertEqual(self.product("GUI-103"), ("point2", "В1", None))

    def test_other_product_label_does_not_override_code_that_point_uses(self):
        self.assertEqual(self.product("GUI-105"), ("point2", "В5", "point3"))
        self.assertEqual(self.product("GUI-106"), ("point2", "В3", "point3"))       # продукт назван в тексте названия
        self.assertIn("И9: Jira: point3", self.review)

    def test_label_that_is_not_a_product_is_ignored(self):
        self.assertEqual(self.product("GUI-104"), ("point2", "В5", None))

    def test_other_product_label_drops_a_task_without_point_code(self):
        from pipeline import units as U
        from tests.helpers import RULES, entry
        e = entry("Добавлена панель отчета", ["monokit", "widgets/report-panel"], "add")
        got = U.decide_product({"entries": [e]}, {"product_tags": ["point3"], "summary": "[point3] Панель"},
                               {e["id"]: {"packages/monokit/widgets/report-panel/ReportPanel.tsx"}}, set(), RULES)
        self.assertEqual((got["value"], got["rule"]), ("other", "В2"))

    def test_tasks_missing_from_changelog_are_listed(self):
        self.assertIn("| GUI-900 | Задача, которой нет в списке изменений | - |", self.review)
        self.assertIn("| Задач Jira в выгрузке | 6 |", self.review)

    def test_queries(self):
        q = (self.out / "jira_query.txt").read_text(encoding="utf-8")
        self.assertIn("\nkey in (GUI-101, GUI-102, GUI-103, GUI-104, GUI-105, GUI-106)\n", q)
        self.assertIn('project in (GUI) AND resolved >= "2026-01-10" AND resolved <= "2026-03-01 23:59"', q)
        self.assertEqual(DRAFT.jira_queries([{"task": "GUI-2"}, {"task": None}], {"lower_date": None, "upper_date": "2026-03-01"}),
                         {"exact": "key in (GUI-2)", "control": ""})


class StaleNamesTest(Workdir):
    """Название исчезло из интерфейса, а документация его упоминает: находится по всему релизу, без привязки к задачам."""

    def setUp(self):
        super().setUp()
        i18n = self.data / P.SLOT_CODE_NEW / "packages/monokit/features/orders-filter/i18n.ts"
        i18n.write_text(i18n.read_text(encoding="utf-8").replace("reset: 'Сбросить', ", ""), encoding="utf-8")
        self.cli("collect")
        self.stale = json.loads((self.out / P.OUT_DATA / "05_stale_texts.json").read_text(encoding="utf-8"))

    def test_found_without_any_task_naming_it(self):
        units = json.loads((self.out / P.OUT_DATA / "05_units.json").read_text(encoding="utf-8"))["units"]
        self.assertFalse(any("Сбросить" in e["text"] for u in units for e in u["entries"]))
        self.assertEqual([(x["lang"], x["text"], x["replacement"]) for x in self.stale], [("rus", "Сбросить", None)])
        self.assertEqual([(m["file"], m["line"]) for m in self.stale[0]["mentions"]], [("user-manual/050_Work.md", 5)])
        self.assertTrue(all("«Сбросить»" in m["text"] for m in self.stale[0]["mentions"]))          # только в кавычках

    def test_shown_in_review_and_given_to_the_agent_as_one_task(self):
        self.assertIn("| rus | «Сбросить» | выяснит агент | `user-manual/050_Work.md` (1) |",
                      (self.out / V.FILE_NAME).read_text(encoding="utf-8"))
        mark_review(self.out)
        self.cli("agent")
        ws = self.out / P.OUT_WORKSPACE
        task = (ws / "tasks/stale_names.md").read_text(encoding="utf-8")
        self.assertIn("### [rus] «Сбросить»", task)
        self.assertIn("`docs/MD_rus/docs/user-manual/050_Work.md`:5", task)
        self.assertIn("сброс: 'Сбросить'", (ws / "sources/STALE/changes.md").read_text(encoding="utf-8"))
        self.assertFalse(any("check_doc" in p.name for p in (ws / "tasks").iterdir()))

    def test_agent_replacement_is_verified_and_applied(self):
        mark_review(self.out)
        self.cli("agent")
        ws = self.out / P.OUT_WORKSPACE
        page = "docs/MD_rus/docs/user-manual/050_Work.md"
        (ws / "answers/stale_names.json").write_text(json.dumps({
            "status": "done", "summary": "кнопки больше нет", "steps": [{"source": "sources/STALE/changes.md", "found": "строка удалена"}],
            "edits": [{"file": page, "find": "Нажмите кнопку «Сбросить», чтобы очистить фильтр.",
                       "replace": "Чтобы очистить фильтр, удалите его значение.", "what": "убрано название кнопки, которой нет",
                       "evidence": [{"source": "i18n", "quote": "удален [ru]: «Сбросить»"}]}]}, ensure_ascii=False), encoding="utf-8")
        self.cli("agent")
        self.assertIn("Чтобы очистить фильтр, удалите его значение.",
                      (self.out / P.OUT_RESULT / "MD_rus/docs/user-manual/050_Work.md").read_text(encoding="utf-8"))
        self.assertIn("| STALE | rus | user-manual/050_Work.md |", (self.out / P.OUT_RESULT / "trace.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
