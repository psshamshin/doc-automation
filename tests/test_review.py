"""Достоверность решений (группа И) и файл проверки человеком review.md."""

import tempfile
import unittest
from pathlib import Path

from pipeline import confidence as CONF
from pipeline import validation as V
from pipeline.layout import Dataset
from tests.helpers import DEMO, RULES, demo_units


class ConfidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.units = demo_units()
        cls.conf = CONF.assess(list(cls.units.values()), RULES)

    def rules_of(self, uid):
        return [f["rule"] for f in self.conf[uid]["flags"]]

    def test_confirmed_by_code_is_high(self):
        for uid in ("GUI-105", "GUI-106", "GUI-104"):
            self.assertEqual(self.conf[uid]["level"], "high", uid)

    def test_dropped_units_are_not_questioned(self):
        self.assertEqual(self.conf["GUI-101"], {"level": "high", "flags": []})
        self.assertEqual(self.conf["GUI-102"]["level"], "high")

    def test_keywords_with_app_code_is_high(self):
        # GUI-103: видимость по словам, но к записи привязан измененный код приложения
        self.assertEqual(self.rules_of("GUI-103"), [])
        self.assertEqual(self.conf["GUI-103"]["level"], "high")

    def test_keywords_without_code_is_low(self):
        u = dict(self.units["GUI-103"])
        u["files"] = []
        self.assertEqual([f["rule"] for f in CONF.assess([u], RULES)[u["id"]]["flags"]], ["И3"])

    def test_unknown_product_is_low(self):
        self.assertIn("И1", self.rules_of("NOTASK:0.10.0#7"))

    def test_shared_strings(self):
        def unit(uid, text):
            u = dict(self.units["GUI-105"])
            u["id"] = uid
            u["entries"] = [dict(u["entries"][0], text=text)]
            return u
        a, b = unit("A-1", "Добавлен фильтр заказов"), unit("B-1", "В фильтре появилась подпись «Дата заказа»")
        conf = CONF.assess([a, b], RULES)
        self.assertEqual([f["rule"] for f in conf["A-1"]["flags"]], ["И4"])
        self.assertEqual(conf["B-1"]["level"], "high")  # текст назван в записи: принадлежит этой задаче

    def test_other_product_files(self):
        u = dict(self.units["GUI-105"])
        u["files"] = [dict(f, reachable_from_app=False) for f in u["files"]]
        self.assertIn("И5", [f["rule"] for f in CONF.assess([u], RULES)[u["id"]]["flags"]])


class ReviewFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.units = list(demo_units().values())
        cls.conf = CONF.assess(cls.units, RULES)
        cls.ds = Dataset(str(DEMO))

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / V.FILE_NAME

    def write(self, **kw):
        return V.write(self.path, self.ds, self.units, self.conf, [], [], 0, **kw)

    def text(self):
        return self.path.read_text(encoding="utf-8")

    def mark(self, uid, column):
        """Переставляет отметку задачи в колонку: 0 - принять, 1 - отклонить, 2 - агенту."""
        lines = self.text().splitlines()
        for i, line in enumerate(lines):
            if line.startswith("|") and uid in line.split("|")[1]:
                cells = line.rstrip("|").split("|")
                cells[-3:] = [" [x] " if n == column else " [ ] " for n in range(3)]
                lines[i] = "|".join(cells) + "|"
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_default_marks(self):
        self.assertEqual(self.write(), "created")
        val = V.parse(self.text())
        self.assertFalse(val["reviewed"])
        self.assertEqual(val["errors"], [])
        self.assertEqual(val["marks"], {
            "GUI-105": "accept", "GUI-106": "accept", "GUI-104": "accept", "GUI-103": "accept",
            "GUI-101": "accept", "GUI-102": "accept", "NOTASK:0.10.0#7": "agent"})
        self.assertEqual(V.decisions(val, self.units), {
            "GUI-105": "doc", "GUI-106": "doc", "GUI-104": "langs", "GUI-103": "rn",
            "GUI-101": "skip", "GUI-102": "skip", "NOTASK:0.10.0#7": "agent"})

    def test_three_choices(self):
        self.write()
        self.mark("GUI-105", 1)   # отклонить задачу, взятую в работу: не брать
        self.mark("GUI-101", 1)   # отклонить отброшенную: проверит агент
        self.mark("GUI-106", 2)   # отдать агенту
        d = V.decisions(V.parse(self.text()), self.units)
        self.assertEqual((d["GUI-105"], d["GUI-101"], d["GUI-106"]), ("skip", "agent", "agent"))

    def test_reviewed_mark_variants(self):
        for mark in ("[x]", "[X]", "[х]", "[ x ]"):   # латинская и русская буква
            self.assertTrue(V.parse(f"Проверено: {mark}\n")["reviewed"], mark)
        self.assertFalse(V.parse("Проверено: [ ]\n")["reviewed"])

    def test_human_marks_survive_rerun(self):
        self.write()
        self.mark("GUI-103", 2)
        self.path.write_text(self.text().replace("Проверено: [ ]", "Проверено: [x]"), encoding="utf-8")
        self.assertEqual(self.write(), "kept")
        val = V.parse(self.text())
        self.assertTrue(val["reviewed"])
        self.assertEqual(val["marks"]["GUI-103"], "agent")

    def test_changed_rules_reset_review(self):
        self.write()
        self.mark("GUI-103", 2)
        text = self.text().replace("Проверено: [ ]", "Проверено: [x]").replace("в доку + RN (В5, Г1, Д2)", "отброшено (В5, Г4, Д2)")
        self.path.write_text(text, encoding="utf-8")
        self.assertEqual(self.write(), "updated")
        val = V.parse(self.text())
        self.assertFalse(val["reviewed"])
        self.assertEqual(val["marks"]["GUI-103"], "agent")   # отметка по неизменившейся задаче сохранена
        self.assertEqual(val["marks"]["GUI-105"], "accept")  # по изменившейся - отметка по умолчанию

    def test_two_marks_in_a_row_is_an_error(self):
        self.write()
        lines = [l.replace("[ ] | [ ] |", "[x] | [ ] |") if "GUI-105" in l else l for l in self.text().splitlines()]
        val = V.parse("\n".join(lines))
        self.assertEqual(len(val["errors"]), 1)
        self.assertNotIn("GUI-105", val["marks"])

    def test_jira_links(self):
        self.assertIsNone(V.task_url("", "GUI-1"))
        self.assertIsNone(V.task_url("https://jira.example/browse/{key}", None))   # запись без задачи
        self.assertEqual(V.task_url("https://jira.example/browse/{key}", "GUI-1"), "https://jira.example/browse/GUI-1")
        self.assertEqual(V.jira_template(self.ds), "")   # в придуманных данных ссылки отключены
        ds = Dataset(str(DEMO))
        ds.overrides = {"jira_task_url": "https://jira.example/browse/{key}"}
        self.assertIn("| [GUI-105](https://jira.example/browse/GUI-105) |", V.render(ds, self.units, self.conf, [], [], 0))


if __name__ == "__main__":
    unittest.main()
