"""Команды пайплайна.

    python3 -m pipeline collect <набор>    сбор изменений, отбор по правилам, черновик
    python3 -m pipeline agent <набор>      агент: проверка решений с низкой достоверностью и текст (после validation.txt)
    python3 -m pipeline compare <набор>    сравнение результата с документацией техписа
    python3 -m pipeline versions <набор>   показать, какие версии определились
    python3 -m pipeline learn <набор>      записать релизы из документации в базу знаний

<набор> - имя папки в input/ или путь к папке с данными.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from . import agent as AG
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


def cmd_collect(ds: Dataset, args) -> None:
    if args.release:
        v = ds.versions
        changed = R.remember(ds.knowledge_path, [{"release": v["release"], "ui": v["ui_new"], "date": v["release_date"]}])
        for c in changed:
            log(f"база знаний: {c}")
    RUN.step_collect(ds)
    RUN.step_classify(ds)
    RUN.step_docs_index(ds)
    RUN.step_workspace(ds, args.force)

    units, conf = _review(ds)
    us = units["units"]
    proposals = {u["id"]: V.proposal(u, conf[u["id"]]) for u in us}
    index = load_json(ds.data_dir / "06_docs_index.json")
    bumps = DRAFT.bump_versions(ds, units["component_versions"])
    rn = DRAFT.write_release_notes(ds, us, proposals, conf, index)
    status = V.write(ds.run_dir / V.FILE_NAME, ds.name, ds.release, us, conf, force=args.force)
    status_text = {"created": "создан, ждет проверки", "kept": "без изменений, ваши правки сохранены",
                   "updated": "обновлен: правила дали другой результат, одобрение снято"}[status]
    jira = load_json(ds.data_dir / "04_jira.json")
    save_json(ds.data_dir / "07_review.json", {"versions": ds.versions, "proposals": proposals, "confidence": conf,
                                              "code_edits": bumps, "release_notes": rn})
    save_text(ds.run_dir / "draft.md", DRAFT.build(ds, us, conf, proposals, bumps, rn, index, len(jira)))
    q = DRAFT.jira_query(us)
    if q:
        save_text(ds.run_dir / "jira_query.txt", q + "\n")
    low = sum(1 for c in conf.values() if c["level"] == "low")
    log(f"черновик: {ds.run_dir / 'draft.md'}")
    log(f"решений с низкой достоверностью: {low} из {len(us)}; файл проверки: {ds.run_dir / V.FILE_NAME} ({status_text})")


def cmd_agent(ds: Dataset, args) -> None:
    vpath = ds.run_dir / V.FILE_NAME
    text = read_text(vpath)
    if text is None:
        raise DatasetError(f"нет файла {vpath}: сначала запустите collect")
    val = V.parse(text)
    if val["errors"]:
        raise DatasetError("ошибки в файле проверки:\n  " + "\n  ".join(val["errors"]))
    if not val["approved"] and not args.approve:
        raise DatasetError(f"решения не одобрены: проверьте {vpath} и поставьте в нем 'approved: yes'")
    units, conf = _review(ds)
    us = units["units"]
    unknown = [u["id"] for u in us if u["id"] not in val["decisions"]]
    if unknown:
        raise DatasetError("в файле проверки нет задач: " + ", ".join(unknown) + ". Перезапустите collect")
    index = load_json(ds.data_dir / "06_docs_index.json")

    def redraft(final):
        DRAFT.write_release_notes(ds, us, final, conf, index)

    res = AG.run(ds, us, conf, val["decisions"], dry_run=args.dry_run, cmd=args.cmd, redo=args.redo,
                 timeout=args.timeout, on_decided=redraft)
    save_text(ds.agent_dir / "summary.md", AG.summary_md(ds, res, val["decisions"]))
    counts = {}
    for i in res["items"]:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    log("задания агента: " + (", ".join(f"{k} - {v}" for k, v in sorted(counts.items())) or "нет"))
    if not res["available"] and not args.dry_run:
        log(f"команда '{res['command'][0]}' не найдена: задания подготовлены в {ds.agent_dir / 'tasks'}, агент не запускался")
    log(f"итог: {ds.agent_dir / 'summary.md'}")
    if any(i["status"] == "failed" for i in res["items"]):
        raise SystemExit(1)


def cmd_compare(ds: Dataset, args) -> None:
    if not ds.has(P.SLOT_DOCS_REFERENCE):
        raise DatasetError(f"в наборе данных нет папки {P.SLOT_DOCS_REFERENCE}: сравнивать не с чем")
    RUN.step_reference(ds)
    RUN.step_compare(ds)


def cmd_versions(ds: Dataset, args) -> None:
    v = ds.versions
    print(f"набор данных:                    {ds.name}")
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


COMMANDS = {"collect": cmd_collect, "agent": cmd_agent, "compare": cmd_compare, "versions": cmd_versions, "learn": cmd_learn}


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="python3 -m pipeline", description="Автоматизация документации LUNA POINT 2")
    ap.add_argument("command", choices=list(COMMANDS))
    ap.add_argument("dataset", help="имя папки в input/ или путь к папке с данными")
    ap.add_argument("--out", help="куда писать результаты (по умолчанию output/<набор>)")
    ap.add_argument("--release", help="версия нового релиза, если она не определилась сама; запоминается в базе знаний")
    ap.add_argument("--force", action="store_true", help="collect: пересоздать рабочую копию документации и файл проверки")
    ap.add_argument("--dry-run", action="store_true", help="agent: только подготовить задания, агента не запускать")
    ap.add_argument("--approve", action="store_true", help="agent: считать решения одобренными без правки validation.txt")
    ap.add_argument("--redo", action="store_true", help="agent: выполнить заново уже выполненные задания")
    ap.add_argument("--cmd", help="agent: команда запуска агента (по умолчанию claude -p ...)")
    ap.add_argument("--timeout", type=int, default=1800, help="agent: секунд на одно задание")
    args = ap.parse_args(argv)
    t0 = time.time()
    try:
        ds = Dataset(args.dataset, out=args.out, release=args.release)
        ds.run_dir.mkdir(parents=True, exist_ok=True)
        COMMANDS[args.command](ds, args)
    except DatasetError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        raise SystemExit(2)
    if args.command not in ("versions",):
        log(f"готово за {time.time() - t0:.1f} с: {ds.run_dir}")


if __name__ == "__main__":
    main()
