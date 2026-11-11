"""Обертка агента: очередь заданий, запуск, проверка ответов.

Агент получает не весь релиз, а отдельные задания:
* decide        - правила не смогли решить или решение под сомнением: решить по коду;
* write_doc     - написать правку документации RU и EN;
* check_doc     - исправление: проверить, не описано ли в документации старое поведение;
* check_langs   - сверить список языков интерфейса;
* release_notes - привести черновик release notes к языку пользователя и перевести.

На каждое задание - отдельный запуск агента с готовым текстом задания. Агент обязан записать ответ
в JSON-файл; без него задание считается невыполненным.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import product as P
from . import validation as V
from .common import ROOT, load_json, log, read_text, save_json, save_text

# Агент не выбран: по умолчанию задания только готовятся. Команда задается параметром --cmd или переменной
# окружения DOC_AGENT_CMD. Она получает текст задания на стандартный ввод и должна записать файл ответа.
# Пример для Claude Code: claude -p --permission-mode acceptEdits --settings agent/settings.json
PROMPTS = ROOT / "agent" / "prompts"
KIND_ORDER = ["decide", "write_doc", "check_doc", "check_langs", "release_notes"]
ANSWER_REQUIRED = ["item", "status", "summary"]
STATUSES = ("done", "needs_human")


def agent_cmd(cmd: Optional[str]) -> List[str]:
    cmd = cmd or os.environ.get("DOC_AGENT_CMD")
    return shlex.split(cmd) if cmd else []


# ---------------------------------------------------------------- очередь

def kinds_for(decision: str, unit: Dict[str, Any]) -> List[str]:
    if decision == "agent":
        return ["decide"]
    if decision == "doc":
        return ["write_doc"]
    if decision == "langs":
        return ["check_langs"]
    if decision == "rn" and unit.get("doc_mentions"):
        return ["check_doc"]
    return []


def build_queue(units: List[Dict[str, Any]], decisions: Dict[str, str], only_kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    items = []
    for u in units:
        for k in kinds_for(decisions.get(u["id"], "skip"), u):
            items.append({"id": f"{u['id']}__{k}".replace(":", "_").replace("#", "_"), "unit": u["id"], "kind": k})
    if any(decisions.get(u["id"]) in ("doc", "rn", "langs") for u in units):
        items.append({"id": "release_notes", "unit": None, "kind": "release_notes"})
    if only_kinds:
        items = [i for i in items if i["kind"] in only_kinds]
    return sorted(items, key=lambda i: (KIND_ORDER.index(i["kind"]), i["id"]))


# ---------------------------------------------------------------- текст задания

def _unit_context(ds, u: Dict[str, Any], conf: Dict[str, Any]) -> str:
    out = ds.rel(ds.run_dir)
    L = [f"## Задача {u['id']}", "", f"Решение правил: {V.rule_text(u)}.", ""]
    flags = conf.get(u["id"], {}).get("flags", [])
    if flags:
        L += ["Основание для проверки:", ""] + [f"- {f['rule']}: {f['text']}" for f in flags] + [""]
    L += ["Записи списка изменений (учитываются только значимые):", ""]
    for e in u["entries"]:
        fl = e["_flags"]
        mark = "значимая" if fl.get("significant") else ("другой продукт" if fl["other_product"] else "тест" if fl["test"] else "техническая")
        L.append(f"- [{mark}] [{e['section']}] [{']['.join(e['tags'])}] {e['text']}")
    rel = [f for f in u["files"] if f.get("relevant")]
    L += ["", f"Файлы значимых записей ({len(rel)}), патчи лежат в `{out}/{P.OUT_DATA}/code/patches/`:", ""]
    for f in rel[:40]:
        note = "" if f["reachable_from_app"] else " (приложение POINT 2 этот файл не использует)"
        L.append(f"- `{f['path']}` [{f['category']}, {f['status']}]{note}" + (f" патч: `{out}/{P.OUT_DATA}/code/{f['patch']}`" if f.get("patch") else ""))
    if len(rel) > 40:
        L.append(f"- ... еще файлов: {len(rel) - 40}, полный список в `{out}/{P.OUT_DATA}/05_units.json`")
    i = u["i18n"]
    if i["added"] or i["removed"] or i["changed"]:
        L += ["", "Тексты интерфейса:", ""]
        L += [f"- добавлен [{x['locale']}]: «{x['value']}»" for x in i["added"][:30]]
        L += [f"- удален [{x['locale']}]: «{x['value']}»" for x in i["removed"][:30]]
        L += [f"- изменен [{x['locale']}]: «{x['old']}» -> «{x['new']}»" for x in i["changed"][:30]]
    if u["doc_candidates"]:
        L += ["", "Страницы-кандидаты (подсказка, не решение): " + ", ".join(f"`{c['doc']}`" for c in u["doc_candidates"])]
    for lang, hits in (u.get("doc_mentions") or {}).items():
        for needle, found in hits.items():
            L.append(f"- старый текст «{needle}» в документации [{lang}]: " + ", ".join(f"`{h['file']}`:{h['line']}" for h in found[:6]))
    return "\n".join(L)


def render_task(ds, item: Dict[str, Any], units_by_id: Dict[str, Any], conf: Dict[str, Any], decisions: Dict[str, str]) -> str:
    out = ds.rel(ds.run_dir)
    answer = f"{out}/{P.OUT_AGENT}/answers/{item['id']}.json"
    v = ds.versions
    common = read_text(PROMPTS / "_common.md") or ""
    body = read_text(PROMPTS / f"{item['kind']}.md") or ""
    ctx = ""
    if item["unit"]:
        ctx = _unit_context(ds, units_by_id[item["unit"]], conf)
    else:
        rows = [f"- {uid}: {d}" for uid, d in decisions.items() if d in ("doc", "rn", "langs")]
        ctx = "## Задачи, которые идут в release notes\n\n" + "\n".join(rows)
    text = "\n\n".join(x.strip() for x in (common, body, ctx) if x.strip()) + "\n"
    repl = {
        "{{ITEM}}": item["id"], "{{UNIT}}": item["unit"] or "", "{{ANSWER_FILE}}": answer, "{{OUT}}": out,
        "{{RESULT}}": f"{out}/{P.OUT_RESULT}", "{{DATA}}": f"{out}/{P.OUT_DATA}", "{{RELEASE}}": v["release"],
        "{{RN_FILE}}": f"{P.RELEASE_NOTES_DIR}/{v['release_notes_file']}",
        "{{RN_PREVIOUS}}": f"{P.RELEASE_NOTES_DIR}/{v['previous_release_notes_file']}",
        "{{CODE_NEW}}": ds.rel(ds.slot(P.SLOT_CODE_NEW)), "{{CODE_OLD}}": ds.rel(ds.slot(P.SLOT_CODE_OLD)),
    }
    for k, val in repl.items():
        text = text.replace(k, val)
    return text


# ---------------------------------------------------------------- ответы

def check_answer(path: Path, item: Dict[str, Any]) -> Dict[str, Any]:
    raw = read_text(path)
    if raw is None:
        return {"ok": False, "error": "агент не записал файл ответа"}
    try:
        a = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"ответ не является JSON: {e}"}
    if not isinstance(a, dict):
        return {"ok": False, "error": "ответ должен быть объектом JSON"}
    miss = [k for k in ANSWER_REQUIRED if k not in a]
    if miss:
        return {"ok": False, "error": "в ответе нет полей: " + ", ".join(miss)}
    if a["status"] not in STATUSES:
        return {"ok": False, "error": f"неизвестный status: {a['status']}"}
    if item["kind"] == "decide" and a["status"] == "done" and a.get("decision") not in ("doc", "rn", "langs", "screens", "skip"):
        return {"ok": False, "error": "для задания decide нужно поле decision: doc, rn, langs, screens или skip"}
    return {"ok": True, "answer": a}


# ---------------------------------------------------------------- запуск

def run_item(ds, item: Dict[str, Any], prompt: str, cmd: List[str], timeout: int) -> Dict[str, Any]:
    adir = ds.agent_dir
    answer = adir / "answers" / f"{item['id']}.json"
    if answer.exists():
        answer.unlink()
    try:
        res = subprocess.run(cmd, input=prompt, text=True, capture_output=True, cwd=str(ROOT), timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"агент не уложился в {timeout} с"}
    save_text(adir / "logs" / f"{item['id']}.log", (res.stdout or "") + ("\n--- stderr ---\n" + res.stderr if res.stderr else ""))
    if res.returncode != 0:
        return {"ok": False, "error": f"агент завершился с кодом {res.returncode}, см. logs/{item['id']}.log"}
    return check_answer(answer, item)


def run(ds, units: List[Dict[str, Any]], conf: Dict[str, Any], decisions: Dict[str, str], *, dry_run: bool,
        cmd: Optional[str], redo: bool, timeout: int, on_decided=None) -> Dict[str, Any]:
    """Сначала задания decide; их ответы дополняют решения, и по ним строится остаток очереди."""
    adir = ds.agent_dir
    by_id = {u["id"]: u for u in units}
    command = agent_cmd(cmd)
    available = bool(command) and shutil.which(command[0]) is not None
    state_path = adir / "queue.json"
    prev = {i["id"]: i for i in (load_json(state_path)["items"] if state_path.is_file() else [])}
    final = dict(decisions)
    done_items: List[Dict[str, Any]] = []

    def process(items: List[Dict[str, Any]]) -> None:
        for n, item in enumerate(items, 1):
            prompt = render_task(ds, item, by_id, conf, final)
            save_text(adir / "tasks" / f"{item['id']}.md", prompt)
            old = prev.get(item["id"], {})
            if old.get("status") in ("done", "needs_human") and not redo and (adir / "answers" / f"{item['id']}.json").is_file():
                item.update(status=old["status"], note="уже выполнено раньше")
            elif dry_run or not available:
                item.update(status="prepared", note="задание подготовлено, агент не запускался")
            else:
                log(f"агент: {item['id']} ({n}/{len(items)})")
                r = run_item(ds, item, prompt, command, timeout)
                if r["ok"]:
                    item.update(status=r["answer"]["status"], note=str(r["answer"]["summary"])[:300])
                else:
                    item.update(status="failed", note=r["error"])
            if item["kind"] == "decide" and item["status"] == "done":
                a = load_json(adir / "answers" / f"{item['id']}.json")
                final[item["unit"]] = a["decision"]
                item["decision"] = a["decision"]
            done_items.append(item)

    process(build_queue(units, decisions, only_kinds=["decide"]))
    if on_decided:
        on_decided(final)
    rest = [i for i in build_queue(units, final) if i["kind"] != "decide"]
    process(rest)
    save_json(state_path, {"command": command, "agent_available": available, "dry_run": dry_run, "items": done_items})
    save_json(adir / "final_decisions.json", final)
    return {"items": done_items, "final": final, "available": available, "command": command}


def summary_md(ds, res: Dict[str, Any], decisions: Dict[str, str]) -> str:
    L = [f"# Работа агента: набор данных {ds.name}", ""]
    if not res["available"]:
        L += [f"Агент не настроен или его команда не найдена: задания только подготовлены в `{P.OUT_AGENT}/tasks/`.", ""]
    L += ["| Задание | Вид | Состояние | Итог |", "| --- | --- | --- | --- |"]
    for i in res["items"]:
        L.append(f"| {i['id']} | {i['kind']} | {i['status']} | {(i.get('note') or '').replace('|', '/')} |")
    changed = {k: v for k, v in res["final"].items() if decisions.get(k) != v}
    if changed:
        L += ["", "Решения агента по задачам с низкой достоверностью:", ""] + [f"- {k}: {v}" for k, v in changed.items()]
    left = [k for k, v in res["final"].items() if v == "agent"]
    if left:
        L += ["", "Остались без решения: " + ", ".join(left) + "."]
    return "\n".join(L) + "\n"
