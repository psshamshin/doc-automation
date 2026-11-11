"""Изменения 7 и 8: файлы и тексты другого продукта не доказательство; исправления оформления."""

import tempfile
import unittest
from pathlib import Path

from pipeline import i18n_extract as I18N
from pipeline import units as U
from tests.helpers import RULES, entry

NO_I18N = {"added": [], "removed": [], "changed": []}


class OtherProductPathTest(unittest.TestCase):
    def test_marker_in_name_and_other_app_folder(self):
        self.assertTrue(U.is_other_product_path("packages/monokit/ui/card/Card.point3.tsx", RULES))
        self.assertTrue(U.is_other_product_path("apps/point3/src/pages/Page.tsx", RULES))
        self.assertFalse(U.is_other_product_path("packages/monokit/ui/card/Card.tsx", RULES))
        self.assertFalse(U.is_other_product_path("apps/point/src/pages/Page.tsx", RULES))


class KeyUsageTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        d = self.root / "packages" / "monokit" / "ui" / "card"
        d.mkdir(parents=True)
        (d / "i18n.ts").write_text("export const ru = { card: { hidden_one: 'x', title: 'y', free: 'z' } };", encoding="utf-8")
        (d / "List.point3.tsx").write_text("t('shared:card.hidden', { count })", encoding="utf-8")
        (d / "Card.tsx").write_text('t("card.title")', encoding="utf-8")
        (d / "Card.point3.tsx").write_text("t(`card.title`)", encoding="utf-8")
        f = "packages/monokit/ui/card/i18n.ts"
        self.rows = [{"file": f, "key": k} for k in ("card.hidden_one", "card.title", "card.free", "single")]
        self.usage = I18N.key_usage(self.root, self.rows, RULES)
        self.f = f

    def test_plural_suffix_and_namespace_prefix(self):
        self.assertEqual(self.usage[f"{self.f}::card.hidden_one"], ["packages/monokit/ui/card/List.point3.tsx"])

    def test_key_used_by_both_products(self):
        self.assertEqual(len(self.usage[f"{self.f}::card.title"]), 2)

    def test_unused_or_too_generic_keys_are_absent(self):
        self.assertNotIn(f"{self.f}::card.free", self.usage)
        self.assertNotIn(f"{self.f}::single", self.usage)


class StyleFixTest(unittest.TestCase):
    def vis(self, entries, files=(), i18n=NO_I18N):
        v = U.decide_visibility(entries, list(files), i18n, RULES)
        return v["value"], v["rule"]

    def test_style_fix_is_screens_only(self):
        e = entry("Исправлены стили блока с именем в карточке: поправлены отступы и шрифт", type_="fix")
        files = [{"path": "packages/monokit/ui/card/index.ts", "category": "source"}]
        self.assertEqual(self.vis([e], files), ("style_only", "Г3"))

    def test_text_change_wins_over_style_words(self):
        e = entry("Исправлены стили блока с именем в карточке", type_="fix")
        i18n = {"added": [{"locale": "ru", "value": "Новый текст"}], "removed": [], "changed": []}
        self.assertEqual(self.vis([e], [], i18n), ("visible", "Г1"))

    def test_not_all_entries_are_style_fixes(self):
        a = entry("Исправлены отступы в форме", type_="fix")
        b = entry("Исправлено сохранение формы", type_="fix", eid="1.0.0#2")
        self.assertEqual(self.vis([a, b]), ("visible", "Г5"))

    def test_style_word_in_new_feature_is_not_a_style_fix(self):
        e = entry("Добавлен выбор цвета рамки в настройках страницы", type_="add")
        self.assertEqual(self.vis([e])[0], "visible")


if __name__ == "__main__":
    unittest.main()
