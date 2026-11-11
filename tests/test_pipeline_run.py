"""Полный прогон pipeline.run на синтетической фикстуре."""

import hashlib
import json
import unittest

from pipeline import run as RUN
from tests.helpers import ROOT

CONFIG = ROOT / "config" / "release-fixture.json"
RUN_DIR = ROOT / "runs" / "fixture"
OUTPUTS = ["01_changelog.json", "02_code_diff.json", "02_reachability.json", "03_i18n_diff.json",
           "03_i18n_rows.json", "04_jira.json", "05_units.json", "06_docs_index.json", "report.md"]


def digests():
    return {n: hashlib.sha256((RUN_DIR / n).read_bytes()).hexdigest() for n in OUTPUTS}


class FullRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        RUN.main(["--config", str(CONFIG), "--force"])
        cls.first = digests()
        cls.units = json.loads((RUN_DIR / "05_units.json").read_text(encoding="utf-8"))
        cls.report = (RUN_DIR / "report.md").read_text(encoding="utf-8")

    def test_outputs_exist(self):
        for n in OUTPUTS:
            self.assertTrue((RUN_DIR / n).is_file(), n)
        self.assertTrue((RUN_DIR / "output" / "MD_rus" / "docs" / "user-manual" / "100_Orders.md").is_file())
        self.assertTrue((RUN_DIR / "output" / "trace.md").is_file())

    def test_decisions(self):
        got = {u["id"]: u["decision"]["action"]["value"] for u in self.units["units"]}
        self.assertEqual(got["GUI-101"], "skip")
        self.assertEqual(got["GUI-102"], "skip")
        self.assertEqual(got["GUI-103"], "rn_and_check_doc")
        self.assertEqual(got["GUI-104"], "rn_and_check_langs")
        self.assertEqual(got["GUI-105"], "doc_and_rn")
        self.assertEqual(got["GUI-106"], "doc_and_rn")

    def test_component_version_found_in_docs(self):
        cv = self.units["component_versions"][0]
        self.assertEqual(cv["doc_mentions"]["rus"][0]["current"], "1.0.0")
        self.assertEqual(cv["doc_mentions"]["eng"][0]["file"], "administrator-manual/055_Deploying.md")

    def test_report(self):
        self.assertIn("| GUI-104 | point2 (В5) | visible (Г1) | Д9 | RN + проверить список языков |", self.report)
        self.assertIn("не учитывается: техническая", self.report)

    def test_no_absolute_paths_in_results(self):
        for n in OUTPUTS:
            self.assertNotIn(str(ROOT), (RUN_DIR / n).read_text(encoding="utf-8"), n)

    def test_rerun_is_identical(self):
        RUN.main(["--config", str(CONFIG), "--steps", "collect,classify,docs-index"])
        self.assertEqual(digests(), self.first)

    def test_etalon_and_compare(self):
        RUN.main(["--config", str(CONFIG), "--steps", "etalon,compare"])
        cmp_ = json.loads((RUN_DIR / "compare" / "compare.json").read_text(encoding="utf-8"))
        self.assertIn("user-manual/100_Orders.md", cmp_["rus"]["only_etalon"])
        self.assertEqual(cmp_["rus"]["only_generated"], [])


class ConfigFilesTest(unittest.TestCase):
    def test_all_json_parse(self):
        for d in ("config", "schemas"):
            for p in sorted((ROOT / d).glob("*.json")):
                json.loads(p.read_text(encoding="utf-8"))

    def test_rule_changes_are_recorded(self):
        rules = json.loads((ROOT / "config" / "rules.json").read_text(encoding="utf-8"))
        doc = (ROOT / "docs" / "RULES.md").read_text(encoding="utf-8")
        for ch in rules["_changes"]:
            for key in ("n", "date", "rules", "old", "new", "reason", "where"):
                self.assertIn(key, ch)
            self.assertIn(f"### Изменение {ch['n']} ", doc)


if __name__ == "__main__":
    unittest.main()
