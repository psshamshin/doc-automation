"""Шаги пайплайна. Запуск - через pipeline/cli.py:

    python3 -m pipeline collect 2.3.0     # сбор, отбор по правилам, черновик
    python3 -m pipeline agent 2.3.0       # агент: решения с низкой достоверностью и текст (после проверки человеком)
    python3 -m pipeline compare 2.3.0     # сравнение с документацией техписа

Все результаты пишутся в output/<набор>/.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from . import changelog as CL
from . import docs as DOCS
from . import i18n_extract as I18N
from . import import_graph as IG
from . import jira as JIRA
from . import product as P
from . import tree_diff as TD
from . import units as U
from .common import load_json, log, read_text, save_json, save_text
from .layout import Dataset
from .report import build_report


def component_versions(ds: Dataset) -> List[Dict[str, str]]:
    v = ds.versions
    return [{"variable": P.UI_VERSION_VARIABLE, "component": P.UI_COMPONENT, "old": v["ui_old"], "new": v["ui_new"]}]


def step_collect(ds: Dataset) -> None:
    data = ds.data_dir
    old, new = ds.slot(P.SLOT_CODE_OLD), ds.slot(P.SLOT_CODE_NEW)
    lower, upper = ds.versions["ui_old"], ds.versions["ui_new"]

    log("1/5 ченджлог")
    cl = CL.collect(new / P.APP_CHANGELOG, lower, upper)
    cl["source"] = ds.rel(cl["source"])
    save_json(data / "01_changelog.json", cl)

    log("2/5 дифф кода")
    code = TD.diff_trees(old, new, ds.rules, data / "code" / "patches")
    code["old_root"], code["new_root"] = ds.rel(old), ds.rel(new)
    save_json(data / "02_code_diff.json", code)

    log("3/5 граф импортов (старая и новая версия)")
    entry = ds.rules["attribution"]["entry_dirs"]
    r_old = IG.reachable_from(old, entry)
    r_new = IG.reachable_from(new, entry)
    save_json(data / "02_reachability.json", {
        "entry_dirs": entry,
        "old_count": len(r_old["files"]), "new_count": len(r_new["files"]),
        "unresolved_old": r_old["unresolved_internal"], "unresolved_new": r_new["unresolved_internal"],
        "files": sorted(set(r_old["files"]) | set(r_new["files"])),
    })

    log("4/5 строки интерфейса")
    rows_old = I18N.extract_tree(old, ds.rules)
    rows_new = I18N.extract_tree(new, ds.rules)
    locs = ds.rules["i18n"]["diff_locales"]
    full = I18N.diff_strings(rows_old, rows_new, locs)
    tr = ds.rules["i18n"]["app_translations_dir"]
    full["app_locale_dirs"] = {"old": I18N.locale_dirs(old, tr), "new": I18N.locale_dirs(new, tr)}
    # где используются новые и измененные тексты (изменение 7: тексты только для другого продукта)
    new_values = {loc: {x["value"] for x in full["locales"][loc]["added"]} for loc in locs}
    changed_keys = {(c["locale"], c["file"], c["key"]) for c in full["changed_same_key"]}
    wanted = [r for r in rows_new if r["locale"] in locs and
              (r["value"] in new_values[r["locale"]] or (r["locale"], r["file"], r["key"]) in changed_keys)]
    save_json(data / "03_i18n_usage.json", I18N.key_usage(new, wanted, ds.rules))
    save_json(data / "03_i18n_rows.json", {"old": rows_old, "new": rows_new})
    save_json(data / "03_i18n_diff.json", full)

    log("5/5 Jira")
    issues = JIRA.load_dir(ds.slot(P.SLOT_JIRA))
    save_json(data / "04_jira.json", issues)


def step_classify(ds: Dataset) -> None:
    data = ds.data_dir
    cl = load_json(data / "01_changelog.json")
    code = load_json(data / "02_code_diff.json")
    reach_doc = load_json(data / "02_reachability.json")
    rows = load_json(data / "03_i18n_rows.json")
    i18n_full = load_json(data / "03_i18n_diff.json")
    jira = load_json(data / "04_jira.json")
    locs = ds.rules["i18n"]["diff_locales"]
    dirs = i18n_full.get("app_locale_dirs", {})
    locale_added = sorted(set(dirs.get("new", [])) - set(dirs.get("old", [])))

    units = U.build_units(cl, code, set(reach_doc["files"]), rows["old"], rows["new"],
                          lambda a, b: I18N.diff_strings(a, b, locs), locale_added, jira, ds.rules, ds.doc_map,
                          i18n_usage=load_json(data / "03_i18n_usage.json"))

    # Е3: где в базовой доке встречаются старые тексты интерфейса
    base = ds.slot(P.SLOT_DOCS_OLD)
    lang_of = ds.rules["i18n"]["doc_lang_of_locale"]
    for u in units:
        if u["decision"]["action"]["value"] == "skip":
            continue
        mentions: Dict[str, Any] = {}
        for loc, lang in lang_of.items():
            needles = [c["old"] for c in u["i18n"]["changed"] if c["locale"] == loc]
            needles += [r["value"] for r in u["i18n"]["removed"] if r["locale"] == loc]
            needles = [n for n in dict.fromkeys(needles) if 4 <= len(n) <= 80 and "$t(" not in n and "{{" not in n][:30]
            if needles:
                hits = DOCS.find_mentions(base, lang, needles)
                if hits:
                    mentions[lang] = hits
        if mentions:
            u["doc_mentions"] = mentions

    # Д10: версии компонентов
    admin = []
    for cv in component_versions(ds):
        hits: Dict[str, List[Dict[str, Any]]] = {}
        for lang in ds.langs:
            root = DOCS.docs_root(base, lang)
            rx = re.compile(re.escape(cv["variable"]) + r"\s*=\s*([\w.\-]+)")
            for p in sorted(root.rglob("*.md")):
                for i, line in enumerate((read_text(p) or "").splitlines(), 1):
                    m = rx.search(line)
                    if m:
                        cur = m.group(1)
                        hits.setdefault(lang, []).append({
                            "file": str(p.relative_to(root)), "line": i, "text": line.strip(), "current": cur,
                            "note": "ок, заменить на новое" if cur == cv["old"] else f"в базе {cur}, ожидалось {cv['old']} — расхождение языков"})
        admin.append({**cv, "doc_mentions": hits})

    unattr = U.unattributed_files(units, code)
    save_json(data / "05_units.json", {"release": ds.release, "units": units, "component_versions": admin,
                                      "unattributed_files": unattr})
    report = build_report(ds, cl, code,
                          {"count": len(reach_doc["files"]), "unresolved": len(reach_doc["unresolved_new"])},
                          i18n_full, jira, units, unattr, admin)
    save_text(ds.run_dir / "report.md", report)
    log(f"задач: {len(units)}; подробный отчет: {ds.run_dir / 'report.md'}")


def step_docs_index(ds: Dataset) -> None:
    idx = DOCS.index_docs(ds.slot(P.SLOT_DOCS_OLD), ds.langs)
    for d in idx.values():
        d["root"] = ds.rel(d["root"])
    save_json(ds.data_dir / "06_docs_index.json", idx)
    log("оглавление документации готово")


def step_workspace(ds: Dataset, force: bool) -> None:
    out = ds.result_dir
    made = DOCS.prepare_workspace(ds.slot(P.SLOT_DOCS_OLD), out, ds.langs, force=force)
    for m in made:
        log(f"рабочая копия документации: {m}")
    tpl = out / "screenshots.md"
    if force or not tpl.exists():
        save_text(tpl, "# Скриншоты к снятию\n\n| Файл доки | Картинка | Действие | Что снять | Задача | Языки |\n| --- | --- | --- | --- | --- | --- |\n")
    rep = out / "trace.md"
    if force or not rep.exists():
        save_text(rep, "# Трассировка: задача → правка\n\n| Задача | Язык | Файл | Что изменено | Правила |\n| --- | --- | --- | --- | --- |\n")


def step_reference(ds: Dataset) -> None:
    v = ds.versions
    d = DOCS.diff_docs(ds.slot(P.SLOT_DOCS_OLD), ds.slot(P.SLOT_DOCS_REFERENCE), ds.langs,
                       f"docs {v['docs_base_version']}", f"docs {ds.release}")
    DOCS.write_diff(d, ds.run_dir / P.OUT_REFERENCE, "reference_diff", f"Что техпис изменил в документации {ds.release}")
    log(f"изменения техписа: {ds.run_dir / P.OUT_REFERENCE}")


def step_compare(ds: Dataset) -> None:
    run, base, eta = ds.run_dir, ds.slot(P.SLOT_DOCS_OLD), ds.slot(P.SLOT_DOCS_REFERENCE)
    ws = ds.result_dir
    gen = DOCS.diff_docs(base, ws, ds.langs, "base", "generated", new_root_fn=DOCS.workspace_root)
    etd = DOCS.diff_docs(base, eta, ds.langs, "base", "reference")
    vs = DOCS.diff_docs(eta, ws, ds.langs, "reference", "generated", new_root_fn=DOCS.workspace_root)
    ignore = {(i["lang"], i["path"]) for i in ds.versions["reference_ignore"]}
    out_dir = run / P.OUT_COMPARE
    DOCS.write_diff(gen, out_dir, "generated_vs_base", "Что изменил пайплайн")
    DOCS.write_diff(vs, out_dir, "generated_vs_reference", "Чем результат отличается от документации техписа")

    L = [f"# Сравнение с документацией техписа {ds.release}\n"]
    summary: Dict[str, Any] = {}
    for lang in ds.langs:
        g = {f["path"] for f in gen["langs"][lang]["files"]}
        e = {f["path"] for f in etd["langs"][lang]["files"] if (lang, f["path"]) not in ignore}
        both, only_g, only_e = sorted(g & e), sorted(g - e), sorted(e - g)
        todo = []
        for p in sorted(DOCS.workspace_root(ws, lang).rglob("*.md")):
            for i, line in enumerate((read_text(p) or "").splitlines(), 1):
                if "SCREENSHOT-TODO" in line:
                    todo.append(f"{p.relative_to(DOCS.workspace_root(ws, lang))}:{i}")
        summary[lang] = {"both": both, "only_generated": only_g, "only_reference": only_e, "screenshot_todo": todo,
                         "reference_images_changed": [i["path"] for i in etd["langs"][lang]["images"]]}
        L.append(f"## {lang}\n")
        L.append(f"- Файлы, измененные и пайплайном, и техписом: {len(both)}: " + ", ".join(f"`{p}`" for p in both))
        L.append(f"- Только пайплайн: {len(only_g)}: " + ", ".join(f"`{p}`" for p in only_g))
        L.append(f"- Только техпис (без учета списка исключений): {len(only_e)}: " + ", ".join(f"`{p}`" for p in only_e))
        L.append(f"- Меток SCREENSHOT-TODO: {len(todo)}; картинок, замененных техписом: {len(summary[lang]['reference_images_changed'])}")
        L.append("")
    L.append("Построчные различия: `generated_vs_reference.html`. Оценку качества текста ставят техпис и ПМ.\n")
    save_json(out_dir / "compare.json", summary)
    save_text(out_dir / "compare.md", "\n".join(L))
    log(f"сравнение: {out_dir / 'compare.md'}")
