"""Сквозные проверки документации: ссылки, названия продуктов, расхождения языков, картинки."""

import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import checks as CHK
from pipeline import cli as CLI
from pipeline import product as P
from pipeline import validation as V


def tree(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, encoding="utf-8")
    return root


class CheckTreeTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        tree(self.root, {
            "index.md": "# Продукт\n\n[Заказы](./user-manual/100_Orders.md)\n[Нет](./user-manual/999_None.md)\n"
                        "[Якорь](./user-manual/100_Orders.md#filters)\n[Нет якоря](./user-manual/100_Orders.md#nothing)\n"
                        "[Сайт](https://example.org/x.md)\n",
            "user-manual/100_Orders.md": "# Заказы\n\n## Фильтры {#filters}\n\n![Экран](../Imgs/image1.png)\n"
                                         "![Нет](../Imgs/image9.png)\n\n{% include \"../includes/010_Table.md\" %}\n"
                                         "{% include \"../includes/999_None.md\" %}\n\nСервис LUNA работает.\n"
                                         "Сервис {{PRODUCT_NAME}} работает.\n```\nLUNA_ENV=1\n```\n",
            "includes/010_Table.md": "| a | b |\n![В таблице](../Imgs/image2.png)\n",
            "includes/020_Orphan.md": "![Картинка](../Imgs/image7.png)\n",
            "release-notes/010.md": "# [0.1.0]\n\nLUNA в названии релиза.\n",
            "user-manual/Imgs/image1.png": b"x", "user-manual/Imgs/image2.png": b"x", "user-manual/Imgs/image3.png": b"x",
        })
        self.res = CHK.check_tree(self.root, self.root, "rus")

    def found(self, key):
        return sorted((x["file"], x["target"], x["why"]) for x in self.res[key])

    def test_broken_links(self):
        self.assertEqual(self.found("links"), [
            ("index.md", "./user-manual/100_Orders.md#nothing", "раздела с таким якорем нет"),
            ("index.md", "./user-manual/999_None.md", "страницы нет"),
            ("user-manual/100_Orders.md", "../includes/999_None.md", "подключаемого файла нет"),
        ])

    def test_product_names_as_plain_text(self):
        # не считаются: переменная документации, блок кода, release notes
        self.assertEqual([(x["file"], x["line"], x["target"]) for x in self.res["brands"]], [("user-manual/100_Orders.md", 11, "LUNA")])

    def test_missing_images(self):
        # страница собирается как папка: '../Imgs' со страницы user-manual/100_Orders.md - это user-manual/Imgs;
        # картинка в подключаемом файле ищется относительно страницы, которая его подключает
        self.assertEqual(self.found("images_missing"), [("user-manual/100_Orders.md", "../Imgs/image9.png", "файла картинки нет")])

    def test_unused_files(self):
        self.assertEqual(self.found("images_unused"), [
            ("includes/020_Orphan.md", "includes/020_Orphan.md", "файл из includes не подключен ни одной страницей"),
            ("user-manual/Imgs/image3.png", "user-manual/Imgs/image3.png", "на картинку нет ссылок"),
        ])


class ParityTest(unittest.TestCase):
    def test_differences_between_languages(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        ru = tree(root / "ru", {"a.md": "# A\n\n## B\n\n| x |\n| y |\n| z |\n\nUI_VER = 2.0.0\n", "only_ru.md": "# R\n",
                                "release-notes/1.md": "# 1\n\n## x\n"})
        en = tree(root / "en", {"a.md": "# A\n\n| x y z |\n\nUI_VER = 1.0.0\n", "release-notes/1.md": "# 1\n"})
        got = sorted((x["file"], x["target"], x["why"]) for x in CHK.check_parity({"rus": ru, "eng": en}))
        self.assertEqual(got, [
            ("a.md", "UI_VER", "версия: rus 2.0.0, eng 1.0.0"),
            ("a.md", "устройство страницы", "разделов: rus 2, eng 1"),      # число строк таблицы не сравнивается
            ("only_ru.md", "страница", "нет в eng"),
        ])


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        from tests.helpers import DEMO
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        shutil.copytree(DEMO, self.data)
        docs = self.data / P.SLOT_DOCS_OLD / "docs/src"
        page = docs / "MD_rus/docs/index.md"
        page.write_text(page.read_text(encoding="utf-8") + "\n[Старая ссылка](./user-manual/999_None.md)\n", encoding="utf-8")
        dep = docs / "MD_eng/docs/administrator-manual/055_Deploying.md"
        dep.write_text(dep.read_text(encoding="utf-8").replace("0.8.0", "0.7.0"), encoding="utf-8")
        self.out = self.tmp / "out"
        CLI.main(["collect", str(self.data), "--out", str(self.out)])

    def test_report_and_review(self):
        checks = (self.out / "checks.md").read_text(encoding="utf-8")
        self.assertIn("| rus | `index.md`:5 | `./user-manual/999_None.md` | страницы нет |  |", checks)
        # в прошлом релизе версии в языках расходились; код выставил одну версию в обоих: расхождение исправлено
        self.assertIn("| Расхождения языков | 0 | 0 | 1 |", checks)
        review = (self.out / V.FILE_NAME).read_text(encoding="utf-8")
        self.assertIn("## Сквозные проверки документации", review)
        self.assertIn("| Битые ссылки | 1 | 0 |", review)

    def test_problem_introduced_in_this_release_is_marked_new(self):
        page = self.out / P.OUT_RESULT / "MD_rus/docs/user-manual/100_Orders.md"
        page.write_text(page.read_text(encoding="utf-8") + "\n[Новая ссылка](./000_None.md)\nВ LUNA добавлен фильтр.\n", encoding="utf-8")
        CLI.main(["check", str(self.data), "--out", str(self.out)])
        checks = (self.out / "checks.md").read_text(encoding="utf-8")
        self.assertIn("| Битые ссылки | 2 | 1 | 0 |", checks)
        self.assertIn("| rus | `user-manual/100_Orders.md`:9 | `./000_None.md` | страницы нет | да |", checks)
        self.assertIn("| Названия продуктов текстом | 1 | 1 | 0 |", checks)


if __name__ == "__main__":
    unittest.main()
