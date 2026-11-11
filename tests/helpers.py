"""Общее для тестов: пути и сборка единиц на синтетической фикстуре (без записи на диск)."""

from pathlib import Path

from pipeline import changelog as CL
from pipeline import i18n_extract as I18N
from pipeline import import_graph as IG
from pipeline import tree_diff as TD
from pipeline import units as U
from pipeline.common import load_json

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
OLD, NEW = FIX / "code-old", FIX / "code-new"
RULES = load_json(ROOT / "config" / "rules.json")
DOC_MAP = load_json(FIX / "doc_map.json")


def entry(text, tags=(), type_="change", task="GUI-1", eid="1.0.0#1"):
    """Запись ченджлога с вычисленными метками и признаками."""
    e = {"id": eid, "task": task, "version": "1.0.0", "section": "", "type": type_, "tags": list(tags),
         "text": text, "code_refs": []}
    e["_markers"] = sorted(U._markers(e, RULES))
    e["_flags"] = U.entry_flags(e, RULES)
    return e


def fixture_units():
    cl = CL.collect(NEW / "apps/point/CHANGELOG.md", "1.0.0", "1.2.0")
    code = TD.diff_trees(OLD, NEW, RULES, None)
    entry_dirs = RULES["attribution"]["entry_dirs"]
    reach = set(IG.reachable_from(OLD, entry_dirs)["files"]) | set(IG.reachable_from(NEW, entry_dirs)["files"])
    rows_old, rows_new = I18N.extract_tree(OLD, RULES), I18N.extract_tree(NEW, RULES)
    locs = RULES["i18n"]["diff_locales"]
    units = U.build_units(cl, code, reach, rows_old, rows_new, lambda a, b: I18N.diff_strings(a, b, locs),
                          [], {}, RULES, DOC_MAP)
    return {u["id"]: u for u in units}
