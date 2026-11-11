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
    shutil.copy(DEMO / "doc_map.json", dst / "doc_map.json")
    shutil.copy(DEMO / "dataset.json", dst / "dataset.json")
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

    def test_missing_code(self):
        root = project(self.tmp, slots=[P.SLOT_CODE_OLD, P.SLOT_DOCS_OLD])
        with self.assertRaises(DatasetError) as cm:
            Dataset("demo", project_root=root)
        self.assertIn("ровно два варианта кода", str(cm.exception))

    def test_any_folder_names_roles_by_version(self):
        root = project(self.tmp)
        d = root / P.INPUT_DIR / "demo"
        (d / P.SLOT_CODE_OLD).rename(d / "zzz-frontend")          # старый код с именем, которое сортируется последним
        (d / P.SLOT_CODE_NEW).rename(d / "aaa-frontend")
        (d / P.SLOT_DOCS_OLD).rename(d / "manual-b")
        (d / P.SLOT_DOCS_REFERENCE).rename(d / "manual-a")
        ds = Dataset("demo", project_root=root)
        self.assertEqual(ds.slot(P.SLOT_CODE_OLD).name, "zzz-frontend")
        self.assertEqual(ds.slot(P.SLOT_CODE_NEW).name, "aaa-frontend")
        self.assertEqual(ds.slot(P.SLOT_DOCS_OLD).name, "manual-b")
        self.assertEqual(ds.slot(P.SLOT_DOCS_REFERENCE).name, "manual-a")
        self.assertEqual((ds.versions["ui_old"], ds.versions["ui_new"]), ("0.8.0", "0.10.0"))

    def test_archives_are_unpacked(self):
        root = project(self.tmp)
        d = root / P.INPUT_DIR / "demo"
        for slot, fmt in ((P.SLOT_CODE_NEW, "zip"), (P.SLOT_DOCS_OLD, "gztar")):
            shutil.make_archive(str(d / f"{slot}-archive"), fmt, root_dir=d, base_dir=slot)
            shutil.rmtree(d / slot)
        ds = Dataset("demo", project_root=root)
        self.assertEqual(sorted(ds.unpacked), sorted([f"{P.SLOT_CODE_NEW}-archive.zip", f"{P.SLOT_DOCS_OLD}-archive.tar.gz"]))
        self.assertIn(P.UNPACKED_DIR, ds.slot(P.SLOT_CODE_NEW).parts)
        self.assertEqual(ds.versions["ui_new"], "0.10.0")
        self.assertEqual(Dataset("demo", project_root=root).unpacked, [])   # второй раз не распаковывает

    def test_archive_with_path_outside_is_rejected(self):
        import zipfile
        root = project(self.tmp)
        with zipfile.ZipFile(root / P.INPUT_DIR / "demo" / "bad.zip", "w") as z:
            z.writestr("../../evil.txt", "x")
        with self.assertRaises(DatasetError):
            Dataset("demo", project_root=root)
        self.assertFalse((root / "evil.txt").exists())

    def test_data_directly_in_input_folder(self):
        root = project(self.tmp)
        inbox = root / P.INPUT_DIR
        for p in list((inbox / "demo").iterdir()):
            p.rename(inbox / p.name)
        (inbox / "demo").rmdir()
        ds = Dataset(None, project_root=root)
        self.assertEqual(ds.dir, inbox.resolve())
        self.assertEqual(ds.name, "0.2.0")   # результаты называются по версии релиза

    def test_single_dataset_needs_no_name(self):
        root = project(self.tmp)
        self.assertEqual(Dataset(None, project_root=root).name, "demo")

    def test_jira_files(self):
        root = project(self.tmp)
        d = root / P.INPUT_DIR / "demo"
        (d / "export.xml").write_text("<rss/>", encoding="utf-8")
        (d / "jira-tasks").mkdir()
        (d / "jira-tasks" / "tasks.txt").write_text("GUI-105 [point] Фильтр по дате", encoding="utf-8")
        self.assertEqual([p.name for p in Dataset("demo", project_root=root).jira_files()], ["export.xml", "tasks.txt"])

    def test_remember(self):
        path = self.tmp / "releases.json"
        self.assertEqual(R.remember(path, [{"release": "0.2.0", "ui": "0.10.0", "date": None}]), ["добавлен релиз 0.2.0"])
        self.assertEqual(R.remember(path, [{"release": "0.2.0", "ui": "0.10.0", "date": "01.03.2026"}]),
                         ["релиз 0.2.0: заполнено поле date"])
        self.assertEqual(R.remember(path, [{"release": "0.2.0", "ui": "0.99.0", "date": "09.09.2026"}]), [])
        self.assertEqual(R.find_by_ui(R.load_knowledge(path), "0.10.0")["date"], "01.03.2026")


if __name__ == "__main__":
    unittest.main()
