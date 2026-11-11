"""Версии определяются из данных, без настроек."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline import product as P
from pipeline import releases as R
from pipeline.layout import Dataset, DatasetError
from tests.helpers import DEMO, NEW, OLD, ROOT


def project(tmp: Path, releases=None, slots=None) -> Path:
    """Временный проект: база знаний с заданным списком релизов и набор данных input/demo."""
    (tmp / P.KNOWLEDGE_DIR).mkdir(parents=True)
    shutil.copy(ROOT / P.RULES_FILE, tmp / P.RULES_FILE)
    shutil.copy(ROOT / P.DOC_MAP_FILE, tmp / P.DOC_MAP_FILE)
    (tmp / P.RELEASES_FILE).write_text(json.dumps({"releases": releases or []}), encoding="utf-8")
    dst = tmp / P.INPUT_DIR / "demo"
    for s in slots or [P.SLOT_CODE_OLD, P.SLOT_CODE_NEW, P.SLOT_DOCS_OLD, P.SLOT_DOCS_REFERENCE]:
        shutil.copytree(DEMO / s, dst / s)
    return tmp


class ReadVersionsTest(unittest.TestCase):
    def test_ui_version_from_package_json(self):
        self.assertEqual(R.ui_version_of(OLD), "0.8.0")
        self.assertEqual(R.ui_version_of(NEW), "0.10.0")

    def test_release_notes_header_and_table(self):
        info = R.parse_release_notes("# [2.2.0] 14.09.2026 {-}\n\n| **Сервис** | **Версия** |\n| --- | --- |\n| point-ui | 2.9.4 |\n")
        self.assertEqual(info["version"], "2.2.0")
        self.assertEqual(info["date"], "14.09.2026")
        self.assertEqual(info["components"], {"point-ui": "2.9.4"})

    def test_release_notes_filename(self):
        self.assertEqual(R.release_notes_filename("2.3.0"), "230.md")
        self.assertEqual(R.release_notes_filename("1.14.1"), "1141.md")

    def test_docs_history(self):
        self.assertEqual([r["version"] for r in R.docs_releases(DEMO / P.SLOT_DOCS_OLD)], ["0.1.0"])


class DatasetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_demo_without_any_settings(self):
        v = Dataset(str(DEMO)).versions
        self.assertEqual((v["ui_old"], v["ui_new"], v["docs_base_version"]), ("0.8.0", "0.10.0", "0.1.0"))
        self.assertEqual((v["release"], v["release_date"]), ("0.2.0", "01.03.2026"))
        self.assertEqual((v["release_notes_file"], v["previous_release_notes_file"]), ("020.md", "010.md"))
        self.assertEqual(v["notes"], [])

    def test_knowledge_base_wins_over_reference(self):
        root = project(self.tmp, [{"release": "0.5.0", "ui": "0.10.0", "date": "02.02.2026"}])
        v = Dataset("demo", project_root=root).versions
        self.assertEqual((v["release"], v["release_date"]), ("0.5.0", "02.02.2026"))
        self.assertIn("база знаний", v["release_source"])

    def test_guess_next_minor_without_reference(self):
        root = project(self.tmp, slots=[P.SLOT_CODE_OLD, P.SLOT_CODE_NEW, P.SLOT_DOCS_OLD])
        v = Dataset("demo", project_root=root).versions
        self.assertEqual(v["release"], "0.2.0")
        self.assertIn("догадка", v["release_source"])
        self.assertIsNone(v["release_date"])
        self.assertEqual(len(v["notes"]), 2)  # версия угадана, дата неизвестна

    def test_release_argument_wins(self):
        root = project(self.tmp, [{"release": "0.5.0", "ui": "0.10.0"}])
        self.assertEqual(Dataset("demo", project_root=root, release="0.7.0").versions["release"], "0.7.0")

    def test_extra_folder_level_is_skipped(self):
        root = project(self.tmp)
        slot = root / P.INPUT_DIR / "demo" / P.SLOT_CODE_NEW
        inner = slot.parent / "unzipped-archive-0.10.0"
        slot.rename(inner)
        slot.mkdir()
        inner.rename(slot / inner.name)
        ds = Dataset("demo", project_root=root)
        self.assertEqual(ds.slot(P.SLOT_CODE_NEW).name, "unzipped-archive-0.10.0")
        self.assertEqual(ds.versions["ui_new"], "0.10.0")

    def test_code_and_docs_from_different_releases(self):
        root = project(self.tmp)
        pj = root / P.INPUT_DIR / "demo" / P.SLOT_CODE_OLD / P.APP_PACKAGE_JSON
        pj.write_text('{"name": "@demoapps/point", "version": "0.7.0"}', encoding="utf-8")
        notes = Dataset("demo", project_root=root).versions["notes"]
        self.assertTrue(any("от одного релиза" in n for n in notes))

    def test_missing_folder(self):
        root = project(self.tmp, slots=[P.SLOT_CODE_OLD, P.SLOT_DOCS_OLD])
        with self.assertRaises(DatasetError) as cm:
            Dataset("demo", project_root=root)
        self.assertIn(P.SLOT_CODE_NEW, str(cm.exception))

    def test_remember(self):
        path = self.tmp / "releases.json"
        self.assertEqual(R.remember(path, [{"release": "0.2.0", "ui": "0.10.0", "date": None}]), ["добавлен релиз 0.2.0"])
        self.assertEqual(R.remember(path, [{"release": "0.2.0", "ui": "0.10.0", "date": "01.03.2026"}]),
                         ["релиз 0.2.0: заполнено поле date"])
        self.assertEqual(R.remember(path, [{"release": "0.2.0", "ui": "0.99.0", "date": "09.09.2026"}]), [])
        self.assertEqual(R.find_by_ui(R.load_knowledge(path), "0.10.0")["date"], "01.03.2026")


if __name__ == "__main__":
    unittest.main()
