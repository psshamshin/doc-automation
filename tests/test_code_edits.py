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

    def test_changelog_when_no_jira(self):
        self.assertEqual(DRAFT.rn_source(self.unit), {"source": "список изменений", "texts": ["Добавлен фильтр заказов по дате"]})

    def test_jira_title_without_product_prefix(self):
        self.unit["jira"] = {"summary": "[point][ui] Фильтр заказов по дате создания"}
        self.assertEqual(DRAFT.rn_source(self.unit), {"source": "Jira", "texts": ["Фильтр заказов по дате создания"]})
        line = DRAFT.rn_block(self.unit, {}, "rus", False)[0]
        self.assertEqual(line, "- **[Общее]**: Фильтр заказов по дате создания <!-- GUI-105, источник: Jira -->")


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
