"""Правила В, Г, Д: признаки записей и решения по единицам на синтетической фикстуре."""

import unittest

from pipeline import units as U
from tests.helpers import RULES, entry, fixture_units


class EntryFlagsTest(unittest.TestCase):
    def test_ui_entry_is_significant(self):
        f = entry("Добавлен фильтр заказов по дате", ["monokit", "features/orders-filter"], "add")["_flags"]
        self.assertTrue(f["ui"] and f["significant"])
        self.assertFalse(f["technical"])

    def test_code_move_with_ui_words_is_technical(self):
        # изменение 2: перенос кода + ссылка на код, слово "страницах" не спасает
        f = entry("Перенесен компонент `SelectBox` в пакет monokit/shared для использования на других страницах")["_flags"]
        self.assertTrue(f["technical"])
        self.assertFalse(f["significant"])

    def test_move_without_code_reference_stays_significant(self):
        f = entry("Кнопка сброса перенесена в меню раздела")["_flags"]
        self.assertFalse(f["technical"])
        self.assertTrue(f["significant"])

    def test_plain_technical_word_needs_no_ui_words(self):
        self.assertTrue(entry("Обновлены зависимости сборки")["_flags"]["technical"])
        self.assertFalse(entry("Обновлены зависимости, исправлена кнопка сохранения")["_flags"]["technical"])

    def test_test_tag(self):
        f = entry("Добавлены тесты фильтра заказов", ["cypress"])["_flags"]
        self.assertTrue(f["test"])
        self.assertFalse(f["significant"])

    def test_point3_marker(self):
        e = entry("Добавлена панель отчета для point3", ["monokit", "widgets/report-panel", "point3"])
        self.assertEqual(e["_markers"], ["point3"])
        self.assertFalse(e["_flags"]["significant"])

    def test_both_products_is_not_other_product(self):
        e = entry("Исправлена прокрутка страницы в Point и Point3")
        self.assertEqual(e["_markers"], [])


class DecideClassTest(unittest.TestCase):
    NO_I18N = {"added": [], "removed": [], "changed": []}

    def klass(self, entries, files=()):
        return U.decide_class({"entries": entries}, list(files), self.NO_I18N, [], RULES)["value"]

    def test_fix_with_removal_words_is_fix(self):
        # изменение 3
        e = entry("Исправлен сброс заголовка страницы. Удалена лишняя обертка", type_="fix")
        self.assertEqual(self.klass([e]), "Д8")

    def test_removal_in_non_fix_entry(self):
        self.assertEqual(self.klass([entry("Удалена кнопка экспорта", type_="change")]), "Д7")
        self.assertEqual(self.klass([entry("Кнопка экспорта больше не показывается", type_="remove")]), "Д7")

    def test_technical_removal_does_not_set_class(self):
        tech = entry("Удален `OldContext`, неиспользуемый код", type_="remove", eid="1.0.0#2")
        fix = entry("Тема больше не сбрасывается при обновлении страницы", type_="fix")
        self.assertEqual(self.klass([fix, tech]), "Д8")

    def test_locale_needs_tag(self):
        # изменение 4
        self.assertEqual(self.klass([entry("Набор локалей расширен: добавлены языки es и pt", ["monokit", "i18n"])]), "Д9")
        self.assertNotEqual(self.klass([entry("Подпись поиска изменена во всех четырех локалях", ["pages", "search"])]), "Д9")

    def test_added(self):
        self.assertEqual(self.klass([entry("Добавлен фильтр заказов по дате", type_="add")]), "Д2")


class ActionTest(unittest.TestCase):
    def act(self, product, visibility, klass):
        return U.action_of({"value": product}, {"value": visibility}, {"value": klass})["value"]

    def test_actions(self):
        self.assertEqual(self.act("other", "visible", "Д2"), "skip")
        self.assertEqual(self.act("point2", "not_visible", "Д9"), "skip")
        self.assertEqual(self.act("point2", "style_only", "Д8"), "screenshot_check")
        self.assertEqual(self.act("point2", "unknown", "Д8"), "review")
        self.assertEqual(self.act("point2", "visible", "Д9"), "rn_and_check_langs")
        self.assertEqual(self.act("point2", "visible", "Д8"), "rn_and_check_doc")
        self.assertEqual(self.act("point2", "visible", "Д3"), "doc_and_rn")


class FixtureUnitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.units = fixture_units()

    def decision(self, uid):
        d = self.units[uid]["decision"]
        return d["product"]["rule"], d["visibility"]["rule"], d["class_draft"]["value"], d["action"]["value"]

    def test_grouping(self):
        self.assertEqual(sorted(self.units), ["GUI-101", "GUI-102", "GUI-103", "GUI-104", "GUI-105", "GUI-106", "NOTASK:1.2.0#7"])
        self.assertEqual(len(self.units["GUI-105"]["entries"]), 2)

    def test_technical_entry_with_i18n_is_dropped(self):
        # GUI-101: перенос компонента, в его файлах есть новые строки переводов
        u = self.units["GUI-101"]
        self.assertTrue(any(f["category"] == "i18n" for f in u["files"]))
        self.assertFalse(any(f["relevant"] for f in u["files"]))
        self.assertEqual(u["i18n"], {"added": [], "removed": [], "changed": []})
        self.assertEqual(self.decision("GUI-101")[1:], ("Г4", "Д3", "skip"))
        self.assertEqual(u["doc_candidates"], [])

    def test_point3_is_dropped(self):
        self.assertEqual(self.decision("GUI-102")[0], "В7")
        self.assertEqual(self.units["GUI-102"]["decision"]["action"]["value"], "skip")

    def test_fix(self):
        self.assertEqual(self.decision("GUI-103"), ("В5", "Г5", "Д8", "rn_and_check_doc"))

    def test_localization(self):
        u = self.units["GUI-104"]
        self.assertEqual(self.decision("GUI-104"), ("В5", "Г1", "Д9", "rn_and_check_langs"))
        self.assertEqual([c["doc"] for c in u["doc_candidates"]], ["user-manual/050_Work.md"])
        flags = [e["_flags"]["significant"] for e in u["entries"]]
        self.assertEqual(flags, [True, False])  # вторая запись - рефакторинг

    def test_visible_feature(self):
        u = self.units["GUI-105"]
        self.assertEqual(self.decision("GUI-105"), ("В5", "Г1", "Д2", "doc_and_rn"))
        self.assertEqual({x["value"] for x in u["i18n"]["added"]}, {"Дата заказа", "Order date"})
        self.assertEqual([c["doc"] for c in u["doc_candidates"]], ["user-manual/100_Orders.md"])
        # файл теста привязан к тестовой записи и в доказательства не идет
        self.assertFalse(any(f["relevant"] for f in u["files"] if f["category"] == "test"))

    def test_navigation(self):
        self.assertEqual(self.decision("GUI-106"), ("В3", "Г2", "Д3", "doc_and_rn"))

    def test_no_task_entry_is_separate_unit(self):
        u = self.units["NOTASK:1.2.0#7"]
        self.assertIsNone(u["task"])
        self.assertEqual(u["decision"]["visibility"]["value"], "not_visible")


if __name__ == "__main__":
    unittest.main()
