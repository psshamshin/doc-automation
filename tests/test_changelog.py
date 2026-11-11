import unittest

from pipeline import changelog as CL
from tests.helpers import NEW


class ParseLineTest(unittest.TestCase):
    def test_tags_task_and_text(self):
        e = CL.parse_line("* [monokit][features/orders-filter] `GUI-105` - Добавлен фильтр `OrdersFilter`")
        self.assertEqual(e["tags"], ["monokit", "features/orders-filter"])
        self.assertEqual(e["task"], "GUI-105")
        self.assertEqual(e["text"], "Добавлен фильтр `OrdersFilter`")
        self.assertEqual(e["code_refs"], ["OrdersFilter"])

    def test_task_in_the_middle_is_not_a_task(self):
        e = CL.parse_line("* [build] Обновлены зависимости сборки после задачи GUI-777")
        self.assertIsNone(e["task"])


class RangeTest(unittest.TestCase):
    def setUp(self):
        self.cl = CL.collect(NEW / "apps/point/CHANGELOG.md", "1.0.0", "1.2.0")

    def test_bounds(self):
        # А2: строго выше нижней границы и не выше верхней
        self.assertEqual([v["version"] for v in self.cl["versions"]], ["1.2.0", "1.1.0"])
        self.assertEqual(sorted(self.cl["outside_range_versions"]), ["1.0.0", "1.3.0"])

    def test_entries(self):
        tasks = [e["task"] for e in self.cl["entries"]]
        self.assertEqual(len(tasks), 9)
        self.assertNotIn("GUI-199", tasks)  # А3: версия 1.3.0 за границей
        self.assertNotIn("GUI-90", tasks)   # нижняя граница не включается
        self.assertIn(None, tasks)          # Б3: запись без задачи

    def test_section_types(self):
        types = {e["task"]: e["type"] for e in self.cl["entries"] if e["task"]}
        self.assertEqual(types["GUI-103"], "fix")
        self.assertEqual(types["GUI-102"], "add")
        self.assertEqual(types["GUI-101"], "change")

    def test_wrapped_line_is_joined(self):
        e = next(e for e in self.cl["entries"] if e["task"] == "GUI-101")
        self.assertIn("использования на других страницах", e["text"])

    def test_semver_order(self):
        sel = CL.select_range([{"version": v, "entries": []} for v in ("2.10.0", "2.9.4", "2.9.10", "2.11.0", "2.11.1")],
                              "2.9.4", "2.11.0")
        self.assertEqual([v["version"] for v in sel], ["2.10.0", "2.9.10", "2.11.0"])


if __name__ == "__main__":
    unittest.main()
