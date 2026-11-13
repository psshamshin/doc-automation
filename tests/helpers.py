"""Общее для тестов: пути и сборка задач на придуманных данных (без записи на диск)."""

from pathlib import Path

from pipeline import changelog as CL
from pipeline import i18n_extract as I18N
from pipeline import import_graph as IG
from pipeline import product as P
from pipeline import tree_diff as TD
from pipeline import units as U
from pipeline.common import load_json

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "tests" / "data" / "demo"
OLD, NEW = DEMO / P.SLOT_CODE_OLD, DEMO / P.SLOT_CODE_NEW
RULES = load_json(ROOT / P.RULES_FILE)
DOC_MAP = load_json(DEMO / "doc_map.json")
UI_OLD, UI_NEW = "0.8.0", "0.10.0"


def entry(text, tags=(), type_="change", task="GUI-1", eid="1.0.0#1"):
    """Запись ченджлога с вычисленными метками и признаками."""
    e = {"id": eid, "task": task, "version": "1.0.0", "section": "", "type": type_, "tags": list(tags),
         "text": text, "code_refs": []}
    e["_markers"] = sorted(U._markers(e, RULES))
    e["_flags"] = U.entry_flags(e, RULES)
    return e


def demo_units():
    cl = CL.collect(NEW / P.APP_CHANGELOG, UI_OLD, UI_NEW)
    code = TD.diff_trees(OLD, NEW, RULES, None)
    entry_dirs = RULES["attribution"]["entry_dirs"]
    reach = set(IG.reachable_from(OLD, entry_dirs)["files"]) | set(IG.reachable_from(NEW, entry_dirs)["files"])
    rows_old, rows_new = I18N.extract_tree(OLD, RULES), I18N.extract_tree(NEW, RULES)
    locs = RULES["i18n"]["diff_locales"]
    units = U.build_units(cl, code, reach, rows_old, rows_new, lambda a, b: I18N.diff_strings(a, b, locs),
                          [], {}, RULES, DOC_MAP)
    return {u["id"]: u for u in units}


# ---------------------------------------------------------------- запуск команд и разметка review.md

import re
import shutil
import sys
import tempfile
import unittest

from pipeline import cli as CLI
from pipeline import validation as V

STUB = f"{sys.executable} {ROOT / 'tests' / 'stub_agent.py'}"


def run(command, out, *extra, data=DEMO):
    CLI.main([command, str(data), "--out", str(out), *extra])


def mark_review(out, **marks):
    """Ставит 'Проверено' и переставляет отметки: 0 - принять, 1 - отклонить, 2 - агенту."""
    p = Path(out) / V.FILE_NAME
    lines = p.read_text(encoding="utf-8").replace("Проверено: [ ]", "Проверено: [x]").splitlines()
    for uid, column in marks.items():
        for i, line in enumerate(lines):
            if line.startswith("|") and uid in line.split("|")[1]:
                cells = line.rstrip("|").split("|")
                cells[-3:] = [" [x] " if n == column else " [ ] " for n in range(3)]
                lines[i] = "|".join(cells) + "|"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


class Workdir(unittest.TestCase):
    """Копия придуманных данных и папка результатов во временной папке."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        shutil.copytree(DEMO, self.data)
        self.out = self.tmp / "out"

    def cli(self, command, *extra):
        CLI.main([command, str(self.data), "--out", str(self.out), *extra])
