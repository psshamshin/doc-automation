"""Команды пайплайна.

    python3 -m pipeline collect <набор>    сбор изменений, отбор по правилам, файл проверки review.md
    python3 -m pipeline agent <набор>      агент: задания, проверка ответов, внесение правок (после проверки review.md)
    python3 -m pipeline check <набор>      сквозные проверки документации (ссылки, языки, картинки, скриншоты)
    python3 -m pipeline compare <набор>    сравнение результата с документацией техписа
    python3 -m pipeline versions <набор>   показать, какие версии определились
    python3 -m pipeline learn <набор>      записать релизы из документации в базу знаний

<набор> - имя папки в input/ или путь к папке с данными.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from typing import List, Optional

from . import agent as AG
from . import checks as CHK
from . import confidence as CONF
from . import draft as DRAFT
from . import product as P
from . import releases as R
from . import run as RUN
from . import validation as V
from .common import load_json, log, read_text, save_json, save_text
from .layout import Dataset, DatasetError


def _review(ds: Dataset):
    units = load_json(ds.data_dir / "05_units.json")
    conf = CONF.assess(units["units"], ds.rules)
    return units, conf


def run_doc_checks(ds: Dataset, decisions: Optional[dict] = None) -> dict:
    """Сквозные проверки результата: пишет checks.md и данные, возвращает итог для файла проверки.

    decisions - решения по задачам: по задачам "только оформление" к проверке идут все скриншоты их страниц.
    """
    res = CHK.run_checks(ds)
    style_pages = {}
    if decisions:
        for u in load_json(ds.data_dir / "05_units.json")["units"]:
            if decisions.get(u["id"]) == "screens":
                for c in u["doc_candidates"]:
                    style_pages.setdefault(c["doc"], u["id"])
    res["screenshots"] = CHK.screenshots_to_review(ds, style_pages)
    save_text(ds.result_dir / "screenshots.md", CHK.render_screenshots(ds, res["screenshots"]))
    save_json(ds.data_dir / "05_doc_checks.json", res)
    save_text(ds.run_dir / "checks.md", CHK.render_md(ds, res))
    return res


def cmd_check(ds: Dataset, args) -> None:
    fpath = ds.agent_dir / "final_decisions.json"
    rpath = ds.data_dir / "07_review.json"
    decisions = load_json(fpath) if fpath.is_file() else (load_json(rpath).get("proposals") if rpath.is_file() else None)
    res = run_doc_checks(ds, decisions)
    for k, title, _ in CHK.CHECKS:
        log(f"{title}: {len(res[k]['items'])}, из них новое: {res[k]['new']}, исправлено: {res[k]['fixed']}")
    log(f"скриншотов к проверке: {len(res['screenshots'])}: {ds.result_dir / 'screenshots.md'}")
    log(f"подробно: {ds.run_dir / 'checks.md'}")


def cmd_collect(ds: Dataset, args) -> None:
    if args.release:
        v = ds.versions
        for c in R.remember(ds.knowledge_path, [{"release": v["release"], "ui": v["ui_new"], "date": v["release_date"]}]):
            log(f"база знаний: {c}")
    if args.force:                           # результат пересоздается: прошлая работа агента к нему уже не относится
        for d in (ds.agent_dir, AG.workspace(ds)):
            shutil.rmtree(d, ignore_errors=True)
    RUN.step_collect(ds)
    RUN.step_classify(ds)
    RUN.step_docs_index(ds)
    RUN.step_workspace(ds, args.force)

    units, conf = _review(ds)
    us = units["units"]
    proposals = V.proposals(us, conf)
    index = load_json(ds.data_dir / "06_docs_index.json")
    bumps = DRAFT.bump_versions(ds, units["component_versions"])
    jira = load_json(ds.data_dir / "04_jira.json")
    status = V.write(ds.run_dir / V.FILE_NAME, ds, us, conf, bumps, len(jira), force=args.force,
                     rn_lines=DRAFT.rn_preview(us, proposals, index), jira_extra=DRAFT.jira_extra(jira, us),
                     stale=load_json(ds.data_dir / "05_stale_texts.json"), checks=run_doc_checks(ds, proposals))
    save_json(ds.data_dir / "07_review.json", {"versions": ds.versions, "proposals": proposals, "confidence": conf,
                                              "code_edits": bumps})
    changelog = load_json(ds.data_dir / "01_changelog.json")
    q = DRAFT.jira_queries(us, changelog)
    if q["exact"]:
        save_text(ds.run_dir / "jira_query.txt", DRAFT.jira_query_file(q, changelog))
    low = sum(1 for c in conf.values() if c["level"] == "low")
    log(f"решений с низкой достоверностью: {low} из {len(us)}")
    log(f"файл проверки: {ds.run_dir / V.FILE_NAME} ({ {'created': 'создан', 'kept': 'отметки сохранены', 'updated': 'решения изменились, отметка Проверено снята'}[status] })")


def cmd_agent(ds: Dataset, args) -> None:
    vpath = ds.run_dir / V.FILE_NAME
    text = read_text(vpath)
    if text is None:
        raise DatasetError(f"нет файла {vpath}: сначала запустите collect")
    val = V.parse(text)
    if val["errors"]:
        raise DatasetError("ошибки в файле проверки:\n  " + "\n  ".join(val["errors"]))
    if not val["reviewed"] and not args.approve:
        raise DatasetError(f"решения не проверены: откройте {vpath} и поставьте 'Проверено: [x]'")
    units, conf = _review(ds)
    us = units["units"]
    unknown = [u["id"] for u in us if u["id"] not in val["marks"]]
    if unknown:
        raise DatasetError("в файле проверки нет задач: " + ", ".join(unknown) + ". Перезапустите collect")
    if AG.agent_cmd(args.cmd) and not args.dry_run:
        exposed = ds.reference_exposed()
        if exposed and not args.allow_reference:
            raise DatasetError(
                "агент не запущен: в проекте лежит документация техписа или результаты сравнения, агент может их прочитать:\n  "
                + "\n  ".join(exposed) + "\nУберите их из папки проекта на время работы агента (документацию техписа - в любое "
                "место вне проекта, потом укажите его: compare --reference <папка>). Запустить все равно: --allow-reference")
    decided = V.decisions(val, us)
    res = AG.run(ds, us, conf, decided, load_json(ds.data_dir / "06_docs_index.json"),
                 load_json(ds.data_dir / "05_stale_texts.json"), dry_run=args.dry_run, cmd=args.cmd, redo=args.redo,
                 timeout=args.timeout)
    save_text(ds.agent_dir / "summary.md", AG.summary_md(ds, res, decided))
    counts: dict = {}
    for i in res["items"]:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    log("задания: " + (", ".join(f"{AG.NAMES[k]} - {v}" for k, v in sorted(counts.items())) or "нет"))
    if counts.get("prepared") and not res["launched"]:
        log(f"дайте агенту задания из {AG.workspace(ds) / 'tasks'} (открыв в редакторе только эту папку), затем запустите agent еще раз")
    run_doc_checks(ds, res["final"])       # после правок агента проверки пересчитываются
    log(f"журнал: {ds.agent_dir / 'summary.md'}")


def cmd_compare(ds: Dataset, args) -> None:
    if args.reference or not ds.has(P.SLOT_DOCS_REFERENCE):
        if not ds.use_reference(args.reference):
            raise DatasetError("сравнивать не с чем: документации нового релиза от техписа нет ни во входных данных, "
                               f"ни в папке {P.REFERENCE_DIR}/. Укажите ее: compare --reference <папка>")
    RUN.step_reference(ds)
    RUN.step_compare(ds)


def cmd_versions(ds: Dataset, args) -> None:
    v = ds.versions
    print(f"набор данных:                    {ds.name}")
    for slot in (P.SLOT_CODE_OLD, P.SLOT_CODE_NEW, P.SLOT_DOCS_OLD, P.SLOT_DOCS_REFERENCE):
        if ds.has(slot):
            print(f"  {P.SLOTS[slot].split(',')[0]}: {ds.slot(slot).relative_to(ds.dir)}")
    print(f"  файлов Jira: {len(ds.jira_files())}")
    print(f"новый релиз:                     {v['release']}  ({v['release_source']})")
    print(f"дата релиза:                     {v['release_date'] or 'неизвестна'}")
    print(f"документация предыдущего релиза: {v['docs_base_version']}")
    print(f"интерфейс:                       {v['ui_old']} -> {v['ui_new']}")
    print(f"файл release notes:              {v['release_notes_file']} (образец: {v['previous_release_notes_file']})")
    for n in v["notes"]:
        print(f"внимание: {n}")


def cmd_learn(ds: Dataset, args) -> None:
    entries = [{"release": r["version"], "ui": r["components"].get(P.UI_COMPONENT), "date": r["date"],
                "components": r["components"]} for r in R.docs_releases(ds.slot(P.SLOT_DOCS_OLD))]
    changed = R.remember(ds.knowledge_path, entries)
    for c in changed:
        log(f"база знаний: {c}")
    log(f"база знаний: {ds.knowledge_path} ({'изменений: ' + str(len(changed)) if changed else 'без изменений'})")


COMMANDS = {"collect": cmd_collect, "check": cmd_check, "agent": cmd_agent, "compare": cmd_compare, "versions": cmd_versions, "learn": cmd_learn}


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="python3 -m pipeline", description="Автоматизация документации LUNA POINT 2")
    ap.add_argument("command", choices=list(COMMANDS))
    ap.add_argument("dataset", nargs="?", help="имя папки в input/ или путь к папке с данными; можно не указывать, если набор один")
    ap.add_argument("--out", help="куда писать результаты (по умолчанию output/<набор>)")
    ap.add_argument("--release", help="версия нового релиза, если она не определилась сама; запоминается в базе знаний")
    ap.add_argument("--force", action="store_true", help="collect: пересоздать рабочую копию документации и файл проверки (стирает правки)")
    ap.add_argument("--dry-run", action="store_true", help="agent: только подготовить задания, агента не запускать")
    ap.add_argument("--approve", action="store_true", help="agent: считать решения проверенными без отметки в review.md")
    ap.add_argument("--redo", action="store_true", help="agent: выполнить заново уже выполненные задания")
    ap.add_argument("--cmd", help="agent: команда запуска агента; текст задания подается ей на стандартный ввод")
    ap.add_argument("--reference", help="compare: папка с документацией техписа, если ее нет во входных данных")
    ap.add_argument("--allow-reference", action="store_true",
                    help="agent: запустить, даже если документация техписа лежит в проекте (результат нельзя считать чистым)")
    ap.add_argument("--timeout", type=int, default=1800, help="agent: секунд на одно задание")
    args = ap.parse_args(argv)
    t0 = time.time()
    try:
        ds = Dataset(args.dataset, out=args.out, release=args.release)
        for a in ds.unpacked:
            log(f"распакован архив: {a}")
        ds.run_dir.mkdir(parents=True, exist_ok=True)
        COMMANDS[args.command](ds, args)
    except DatasetError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        raise SystemExit(2)
    if args.command not in ("versions",):
        log(f"готово за {time.time() - t0:.1f} с: {ds.run_dir}")


if __name__ == "__main__":
    main()
