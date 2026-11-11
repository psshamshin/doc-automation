"""CLI пайплайна.

    python3 -m pipeline.run --config config/release-2.3.0.json            # collect, classify, docs-index, workspace
    python3 -m pipeline.run --config config/release-2.3.0.json --steps collect,classify
    python3 -m pipeline.run --config config/release-2.3.0.json --steps etalon   # дифф эталона (только для шага 7)
    python3 -m pipeline.run --config config/release-2.3.0.json --steps compare  # сравнение результата с эталоном

Все результаты пишутся в runs/<release>/.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from . import changelog as CL
from . import docs as DOCS
from . import i18n_extract as I18N
from . import import_graph as IG
from . import jira as JIRA
from . import tree_diff as TD
from . import units as U
from .common import Config, load_json, log, read_text, save_json, save_text
from .report import build_report

DEFAULT_STEPS = ["collect", "classify", "docs-index", "workspace"]


def _rel(cfg: Config, p) -> str:
    """Путь относительно каталога doc-automation: результаты не зависят от того, где лежит проект."""
    try:
        return Path(p).resolve().relative_to(cfg.base.resolve()).as_posix()
    except ValueError:
        return str(p)


def step_collect(cfg: Config) -> None:
    run = cfg.run_dir
    old, new = cfg.path_of("lerna_old"), cfg.path_of("lerna_new")
    lower, upper = cfg.get("ui_version_lower"), cfg.get("ui_version_upper")

    log("1/5 ченджлог")
    cl = CL.collect(new / cfg.get("changelog", "apps/point/CHANGELOG.md"), lower, upper)
    cl["source"] = _rel(cfg, cl["source"])
    save_json(run / "01_changelog.json", cl)

    log("2/5 дифф кода")
    code = TD.diff_trees(old, new, cfg.rules, run / "code" / "patches")
    code["old_root"], code["new_root"] = _rel(cfg, old), _rel(cfg, new)
    save_json(run / "02_code_diff.json", code)

    log("3/5 граф импортов (старая и новая версия)")
    entry = cfg.rules["attribution"]["entry_dirs"]
    r_old = IG.reachable_from(old, entry)
    r_new = IG.reachable_from(new, entry)
    save_json(run / "02_reachability.json", {
        "entry_dirs": entry,
        "old_count": len(r_old["files"]), "new_count": len(r_new["files"]),
        "unresolved_old": r_old["unresolved_internal"], "unresolved_new": r_new["unresolved_internal"],
        "files": sorted(set(r_old["files"]) | set(r_new["files"])),
    })

    log("4/5 строки интерфейса")
    rows_old = I18N.extract_tree(old, cfg.rules)
    rows_new = I18N.extract_tree(new, cfg.rules)
    locs = cfg.rules["i18n"]["diff_locales"]
    full = I18N.diff_strings(rows_old, rows_new, locs)
    tr = cfg.rules["i18n"]["app_translations_dir"]
    full["app_locale_dirs"] = {"old": I18N.locale_dirs(old, tr), "new": I18N.locale_dirs(new, tr)}
    save_json(run / "03_i18n_rows.json", {"old": rows_old, "new": rows_new})
    save_json(run / "03_i18n_diff.json", full)

    log("5/5 Jira")
    issues = JIRA.load_dir(cfg.path_of("jira"))
    save_json(run / "04_jira.json", issues)


def step_classify(cfg: Config) -> None:
    run = cfg.run_dir
    cl = load_json(run / "01_changelog.json")
    code = load_json(run / "02_code_diff.json")
    reach_doc = load_json(run / "02_reachability.json")
    rows = load_json(run / "03_i18n_rows.json")
    i18n_full = load_json(run / "03_i18n_diff.json")
    jira = load_json(run / "04_jira.json")
    locs = cfg.rules["i18n"]["diff_locales"]
    dirs = i18n_full.get("app_locale_dirs", {})
    locale_added = sorted(set(dirs.get("new", [])) - set(dirs.get("old", [])))

    units = U.build_units(cl, code, set(reach_doc["files"]), rows["old"], rows["new"],
                          lambda a, b: I18N.diff_strings(a, b, locs), locale_added, jira, cfg.rules, cfg.doc_map)

    # Е3: где в базовой доке встречаются старые тексты интерфейса
    base = cfg.path_of("docs_base")
    lang_of = cfg.rules["i18n"]["doc_lang_of_locale"]
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
    for cv in cfg.get("component_versions", []):
        hits: Dict[str, List[Dict[str, Any]]] = {}
        for lang in cfg.langs:
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
    save_json(run / "05_units.json", {"release": cfg.release, "units": units, "component_versions": admin,
                                     "unattributed_files": unattr})
    report = build_report(cfg, cl, code,
                          {"count": len(reach_doc["files"]), "unresolved": len(reach_doc["unresolved_new"])},
                          i18n_full, jira, units, unattr, admin)
    save_text(run / "report.md", report)
    log(f"единиц: {len(units)}; отчёт: {run / 'report.md'}")


def step_docs_index(cfg: Config) -> None:
    idx = DOCS.index_docs(cfg.path_of("docs_base"), cfg.langs)
    for d in idx.values():
        d["root"] = _rel(cfg, d["root"])
    save_json(cfg.run_dir / "06_docs_index.json", idx)
    log("индекс базовой доки готов")


def step_workspace(cfg: Config, force: bool) -> None:
    out = cfg.run_dir / "output"
    made = DOCS.prepare_workspace(cfg.path_of("docs_base"), out, cfg.langs, force=force)
    for m in made:
        log(f"рабочая папка: {m}")
    tpl = out / "screenshots.md"
    if not tpl.exists():
        save_text(tpl, "# Скриншоты к снятию\n\n| Файл доки | Картинка | Действие | Что снять | Задача | Языки |\n| --- | --- | --- | --- | --- | --- |\n")
    rep = out / "trace.md"
    if not rep.exists():
        save_text(rep, "# Трассировка: задача → правка\n\n| Задача | Язык | Файл | Что изменено | Правила |\n| --- | --- | --- | --- | --- |\n")


def step_etalon(cfg: Config) -> None:
    d = DOCS.diff_docs(cfg.path_of("docs_base"), cfg.path_of("docs_etalon"), cfg.langs,
                       f"docs {cfg.get('docs_base_version')}", f"docs {cfg.release}")
    DOCS.write_diff(d, cfg.run_dir / "_etalon", "etalon_diff", f"Эталон: что техпис изменил в доке {cfg.release}")
    log(f"дифф эталона: {cfg.run_dir / '_etalon'}")


def step_compare(cfg: Config) -> None:
    run, base, eta = cfg.run_dir, cfg.path_of("docs_base"), cfg.path_of("docs_etalon")
    ws = run / "output"
    gen = DOCS.diff_docs(base, ws, cfg.langs, "base", "generated", new_root_fn=DOCS.workspace_root)
    etd = DOCS.diff_docs(base, eta, cfg.langs, "base", "etalon")
    vs = DOCS.diff_docs(eta, ws, cfg.langs, "etalon", "generated", new_root_fn=DOCS.workspace_root)
    ignore = {(i["lang"], i["path"]) for i in cfg.get("etalon_ignore", [])}
    out_dir = run / "compare"
    DOCS.write_diff(gen, out_dir, "generated_vs_base", "Что изменил пайплайн")
    DOCS.write_diff(vs, out_dir, "generated_vs_etalon", "Чем результат отличается от эталона")

    L = [f"# Сравнение с эталоном {cfg.release}\n"]
    summary: Dict[str, Any] = {}
    for lang in cfg.langs:
        g = {f["path"] for f in gen["langs"][lang]["files"]}
        e = {f["path"] for f in etd["langs"][lang]["files"] if (lang, f["path"]) not in ignore}
        both, only_g, only_e = sorted(g & e), sorted(g - e), sorted(e - g)
        todo = []
        for p in sorted(DOCS.workspace_root(ws, lang).rglob("*.md")):
            for i, line in enumerate((read_text(p) or "").splitlines(), 1):
                if "SCREENSHOT-TODO" in line:
                    todo.append(f"{p.relative_to(DOCS.workspace_root(ws, lang))}:{i}")
        summary[lang] = {"both": both, "only_generated": only_g, "only_etalon": only_e, "screenshot_todo": todo,
                         "etalon_images_changed": [i["path"] for i in etd["langs"][lang]["images"]]}
        L.append(f"## {lang}\n")
        L.append(f"- Файлы, изменённые и пайплайном, и техписом: {len(both)} — " + ", ".join(f"`{p}`" for p in both))
        L.append(f"- Только пайплайн: {len(only_g)} — " + ", ".join(f"`{p}`" for p in only_g))
        L.append(f"- Только техпис (без учёта etalon_ignore): {len(only_e)} — " + ", ".join(f"`{p}`" for p in only_e))
        L.append(f"- Меток SCREENSHOT-TODO: {len(todo)}; картинок, заменённых техписом: {len(summary[lang]['etalon_images_changed'])}")
        L.append("")
    L.append("Построчные различия: `generated_vs_etalon.html`. Оценку качества текста ставят техпис и ПМ.\n")
    save_json(out_dir / "compare.json", summary)
    save_text(out_dir / "compare.md", "\n".join(L))
    log(f"сравнение: {out_dir / 'compare.md'}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Пайплайн автоматизации документации LUNA POINT 2")
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--steps", default=",".join(DEFAULT_STEPS),
                    help="через запятую: collect, classify, docs-index, workspace, etalon, compare")
    ap.add_argument("--force", action="store_true", help="пересоздать рабочую папку output/")
    args = ap.parse_args(argv)
    cfg = Config(args.config)
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    t0 = time.time()
    for s in steps:
        if s == "collect":
            step_collect(cfg)
        elif s == "classify":
            step_classify(cfg)
        elif s == "docs-index":
            step_docs_index(cfg)
        elif s == "workspace":
            step_workspace(cfg, args.force)
        elif s == "etalon":
            step_etalon(cfg)
        elif s == "compare":
            step_compare(cfg)
        else:
            raise SystemExit(f"неизвестный шаг: {s}")
    log(f"готово за {time.time() - t0:.1f} с → {cfg.run_dir}")


if __name__ == "__main__":
    main()
