"""Достоверность решений (группа И) и файл проверки человеком."""

import tempfile
import unittest
from pathlib import Path

from pipeline import confidence as CONF
from pipeline import validation as V
from tests.helpers import RULES, demo_units


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

    def test_keywords_only_is_low(self):
        self.assertEqual(self.rules_of("GUI-103"), ["И3"])
        self.assertEqual(self.conf["GUI-103"]["level"], "low")

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


class ValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.units = list(demo_units().values())
        cls.conf = CONF.assess(cls.units, RULES)

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / V.FILE_NAME

    def write(self, **kw):
        return V.write(self.path, "demo", "0.2.0", self.units, self.conf, **kw)

    def test_proposals(self):
        self.assertEqual(self.write(), "created")
        val = V.parse(self.path.read_text(encoding="utf-8"))
        self.assertFalse(val["approved"])
        self.assertEqual(val["errors"], [])
        self.assertEqual(val["decisions"], {
            "GUI-105": "doc", "GUI-106": "doc", "GUI-104": "langs", "GUI-103": "agent",
            "GUI-101": "skip", "GUI-102": "skip", "NOTASK:0.10.0#7": "agent"})

    def test_human_edits_survive_rerun(self):
        self.write()
        text = self.path.read_text(encoding="utf-8").replace("approved: no", "approved: yes").replace("GUI-103 | agent", "GUI-103 | rn")
        self.path.write_text(text, encoding="utf-8")
        self.assertEqual(self.write(), "kept")
        val = V.parse(self.path.read_text(encoding="utf-8"))
        self.assertTrue(val["approved"])
        self.assertEqual(val["decisions"]["GUI-103"], "rn")

    def test_changed_rules_reset_approval(self):
        self.write()
        text = self.path.read_text(encoding="utf-8").replace("approved: no", "approved: yes").replace("GUI-103 | agent", "GUI-103 | rn")
        self.path.write_text(text.replace("GUI-105 | doc | в доку + RN", "GUI-105 | doc | отброшено"), encoding="utf-8")
        self.assertEqual(self.write(), "updated")
        val = V.parse(self.path.read_text(encoding="utf-8"))
        self.assertFalse(val["approved"])
        self.assertEqual(val["decisions"]["GUI-103"], "rn")  # решение человека по неизменившейся задаче сохранено

    def test_unknown_decision_is_an_error(self):
        val = V.parse("approved: yes\nGUI-1 | maybe | x |\n")
        self.assertEqual(len(val["errors"]), 1)
        self.assertEqual(val["decisions"], {})


if __name__ == "__main__":
    unittest.main()
