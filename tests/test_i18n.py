import unittest

from pipeline import i18n_extract as I18N
from tests.helpers import NEW, OLD, RULES

NAMES = RULES["i18n"]["locale_export_names"]


def rows(src, rel="packages/x/i18n.ts"):
    return {(r["locale"], r["key"]): r["value"] for r in I18N.extract_file(src, NAMES, rel)}


class ExtractTest(unittest.TestCase):
    def test_locale_by_export_name(self):
        r = rows("export const ruRu = { a: { b: 'Текст' } };\nexport const enUs = { a: { b: 'Text' } };")
        self.assertEqual(r[("ru", "a.b")], "Текст")
        self.assertEqual(r[("en", "a.b")], "Text")

    def test_cyrillic_keys(self):
        r = rows("export const ruRu = { 'Статус': 'Статус', сброс: 'Сбросить' };")
        self.assertEqual(set(r.values()), {"Статус", "Сбросить"})
        self.assertTrue(all(loc == "ru" for loc, _ in r))

    def test_merge_call(self):
        r = rows("const base = { reset: 'Reset' };\nexport const enUs = merge({}, base, { status: 'Status' });")
        self.assertEqual(r[("en", "status")], "Status")

    def test_locale_from_directory(self):
        got = I18N.extract_file("export const data = { a: 'Texto' };", NAMES, "apps/point/src/app/translations/es/data.ts")
        self.assertEqual([x["locale"] for x in got], ["es"])


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.d = I18N.diff_strings(I18N.extract_tree(OLD, RULES), I18N.extract_tree(NEW, RULES), ["ru", "en"])

    def test_added(self):
        ru = {x["value"] for x in self.d["locales"]["ru"]["added"]}
        en = {x["value"] for x in self.d["locales"]["en"]["added"]}
        self.assertEqual(ru, {"Дата заказа", "Выберите значение", "Испанский", "Португальский"})
        self.assertEqual(en, {"Order date", "Select a value", "Spanish", "Portuguese"})

    def test_key_rename_is_not_a_text_change(self):
        # 'Статус' -> status, сброс -> reset: ключи латинизированы, тексты те же
        self.assertEqual(self.d["locales"]["ru"]["removed"], [])
        self.assertEqual(self.d["changed_same_key"], [])

    def test_reference_values_are_ignored(self):
        values = {x["value"] for loc in self.d["locales"].values() for x in loc["added"] + loc["removed"]}
        self.assertFalse(any("$t(" in v for v in values))


if __name__ == "__main__":
    unittest.main()
