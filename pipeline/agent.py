"""Обертка агента: изолированная рабочая папка, задания, проверка ответов, журнал.

Агент не работает в папке проекта. Для него собирается отдельная папка output/<набор>/agent_workspace/,
в которой есть только то, что нужно для заданий: сами задания, рабочая копия документации и источники
каждой задачи (патчи и файлы кода). Правил отбора, кода пайплайна, чужих данных и документации техписа
там нет.

Что делает код вокруг агента:
* проверяет ответ: каждое утверждение должно быть подтверждено дословной цитатой из источников задачи;
* принимает правки документации только из подтвержденных ответов, остальные откладывает в agent/rejected/;
* ведет журнал agent/summary.md: что решено, что принято, что отклонено и почему.

Виды заданий: decide (проверить решение), write_doc (правка RU и EN), check_doc (исправление: проверить
документацию), check_langs (сверить список языков), release_notes (довести черновик).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import docs as DOCS
from . import product as P
from . import validation as V
from .common import ROOT, load_json, log, read_text, save_json, save_text

# Агент не выбран: по умолчанию задания только готовятся. Команда задается параметром --cmd или переменной
# окружения DOC_AGENT_CMD. Она запускается в рабочей папке агента, получает текст задания на стандартный
# ввод и должна записать файл ответа.
PROMPTS = ROOT / "agent" / "prompts"
KIND_ORDER = ["decide", "write_doc", "check_doc", "check_langs", "release_notes"]
ANSWER_REQUIRED = ["item", "status", "summary"]
STATUSES = ("done", "needs_human")
NEED_EVIDENCE = ("decide", "write_doc", "check_doc", "check_langs")
VIRTUAL_SOURCES = ("changelog", "i18n", "jira")
LOG_FILES = ("docs/trace.md", "docs/screenshots.md")
MAX_CODE_FILES = 80
LANG_SETTINGS = ("apps/point/package.json", "apps/point/src/app/translations/i18n.ts")


def agent_cmd(cmd: Optional[str]) -> List[str]:
    cmd = cmd or os.environ.get("DOC_AGENT_CMD")
    return shlex.split(cmd) if cmd else []


def workspace(ds) -> Path:
    return ds.run_dir / P.OUT_WORKSPACE


def unit_dir(uid: str) -> str:
    return uid.replace(":", "_").replace("#", "_")


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
            items.append({"id": f"{unit_dir(u['id'])}__{k}", "unit": u["id"], "kind": k})
    if any(decisions.get(u["id"]) in ("doc", "rn", "langs") for u in units):
        items.append({"id": "release_notes", "unit": None, "kind": "release_notes"})
    if only_kinds:
        items = [i for i in items if i["kind"] in only_kinds]
    return sorted(items, key=lambda i: (KIND_ORDER.index(i["kind"]), i["id"]))


# ---------------------------------------------------------------- источники задачи

def unit_sources(ds, u: Dict[str, Any], kind: str = "") -> Dict[str, Path]:
    """Источники задачи: путь в рабочей папке агента -> настоящий файл. Только то, что привязано к задаче."""
    out: Dict[str, Path] = {}
    base = f"sources/{unit_dir(u['id'])}"
    data = ds.data_dir / "code"
    new = ds.slot(P.SLOT_CODE_NEW)
    rel = [f for f in u["files"] if f.get("relevant")]
    for f in rel:
        if f.get("patch"):
            out[f"{base}/patches/{Path(f['patch']).name}"] = data / f["patch"]
    for f in rel[:MAX_CODE_FILES]:
        if (new / f["path"]).is_file() and not f.get("binary"):
            out[f"{base}/code/{f['path']}"] = new / f["path"]
    if kind == "check_langs":
        for p in LANG_SETTINGS:
            if (new / p).is_file():
                out[f"{base}/code/{p}"] = new / p
    return out


def unit_pages(ds, u: Dict[str, Any]) -> List[str]:
    """Страницы документации задачи в рабочей папке агента: кандидаты и места со старыми текстами."""
    pages = {c["doc"] for c in u["doc_candidates"]}
    pages |= {h["file"] for hits in (u.get("doc_mentions") or {}).values() for found in hits.values() for h in found}
    out = []
    for lang in ds.langs:
        for page in sorted(pages):
            if (ds.result_dir / f"MD_{lang}" / "docs" / page).is_file():
                out.append(f"docs/MD_{lang}/docs/{page}")
    return out


# ---------------------------------------------------------------- текст задания

def _unit_context(ds, u: Dict[str, Any], conf: Dict[str, Any], kind: str) -> str:
    L = [f"## Задача {u['id']}", "", f"Решение правил: {V.rule_text(u)}.", ""]
    flags = conf.get(u["id"], {}).get("flags", [])
    if flags:
        L += ["Основание для проверки:", ""] + [f"- {f['rule']}: {f['text']}" for f in flags] + [""]
    L += ["Записи списка изменений (учитываются только значимые):", ""]
    for e in u["entries"]:
        fl = e["_flags"]
        mark = "значимая" if fl.get("significant") else ("другой продукт" if fl["other_product"] else "тест" if fl["test"] else "техническая")
        L.append(f"- [{mark}] [{e['section']}] [{']['.join(e['tags'])}] {e['text']}")
    i = u["i18n"]
    if i["added"] or i["removed"] or i["changed"]:
        L += ["", "Тексты интерфейса:", ""]
        L += [f"- добавлен [{x['locale']}]: «{x['value']}»" for x in i["added"][:40]]
        L += [f"- удален [{x['locale']}]: «{x['value']}»" for x in i["removed"][:40]]
        L += [f"- изменен [{x['locale']}]: «{x['old']}» -> «{x['new']}»" for x in i["changed"][:40]]
    j = u.get("jira")
    if j:
        L += ["", f"Задача в Jira: {j.get('summary') or ''}", "", (j.get("description") or "")[:1500]]
    for lang, hits in (u.get("doc_mentions") or {}).items():
        for needle, found in hits.items():
            L.append(f"- старый текст «{needle}» в документации [{lang}]: "
                     + ", ".join(f"`docs/MD_{lang}/docs/{h['file']}`:{h['line']}" for h in found[:6]))
    pages = unit_pages(ds, u)
    L += ["", "## Страницы документации этой задачи", ""]
    L += [f"- `{p}`" for p in pages] or ["Страницы не определены: если без них задание не выполнить, верни `needs_human`."]
    L += ["", "## Разрешенные источники", "",
          "Открывай только эти файлы и страницы из списка выше. В подтверждениях указывай источник точно так, "
          "как он записан здесь.", "",
          "- `changelog` - записи списка изменений этой задачи (выше)",
          "- `i18n` - тексты интерфейса этой задачи (выше)"]
    if j:
        L.append("- `jira` - название и описание задачи в Jira (выше)")
    srcs = unit_sources(ds, u, kind)
    L += [f"- `{s}`" for s in srcs]
    if not srcs:
        L.append("- файлов кода у значимых записей этой задачи нет")
    return "\n".join(L)


def rules_text(ds) -> str:
    """Общие правила для агента (AGENTS.md его рабочей папки) без списка заданий."""
    return (read_text(PROMPTS / "workspace_agents.md") or "").replace("{{RELEASE}}", ds.versions["release"])


def rules_code(ds) -> str:
    """Код общих правил. Агент обязан повторить его в ответе: так видно, что правила он прочитал."""
    return "R-" + hashlib.sha256(rules_text(ds).replace("{{RULES_CODE}}", "").encode("utf-8")).hexdigest()[:8]


def render_task(ds, item: Dict[str, Any], units_by_id: Dict[str, Any], conf: Dict[str, Any], decisions: Dict[str, str]) -> str:
    """Задание: только данные конкретной задачи. Общие правила и порядок действий лежат в AGENTS.md."""
    v = ds.versions
    head = read_text(PROMPTS / "task.md") or ""
    if item["unit"]:
        ctx = _unit_context(ds, units_by_id[item["unit"]], conf, item["kind"])
    else:
        rows = [f"- {uid}: {V.DECISION_TEXT[d]}" for uid, d in decisions.items() if d in ("doc", "rn", "langs")]
        rn = f"{P.RELEASE_NOTES_DIR}/{v['release_notes_file']}"
        prev = f"{P.RELEASE_NOTES_DIR}/{v['previous_release_notes_file']}"
        ctx = ("## Файлы\n\n" + "\n".join(f"- черновик: `docs/MD_{lang}/docs/{rn}`" for lang in ds.langs) + "\n"
               + "\n".join(f"- образец прошлого релиза: `docs/MD_{lang}/docs/{prev}`" for lang in ds.langs)
               + "\n\n## Задачи, которые идут в release notes\n\n" + "\n".join(rows))
    text = head.strip() + "\n\n" + ctx.strip() + "\n"
    for k, val in {"{{ITEM}}": item["id"], "{{KIND}}": item["kind"], "{{ANSWER_FILE}}": f"answers/{item['id']}.json"}.items():
        text = text.replace(k, val)
    return text


# ---------------------------------------------------------------- проверка ответа

def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _virtual_text(u: Dict[str, Any], source: str) -> str:
    if source == "changelog":
        return " \n ".join(e["text"] for e in u["entries"])
    if source == "i18n":
        i = u["i18n"]
        vals = [x["value"] for x in i["added"] + i["removed"]] + [y for c in i["changed"] for y in (c["old"], c["new"])]
        return " \n ".join(vals)
    j = u.get("jira") or {}
    return f"{j.get('summary') or ''} \n {j.get('description') or ''}"


def verify_evidence(ds, u: Dict[str, Any], kind: str, answer: Dict[str, Any]) -> List[str]:
    """Каждое утверждение агента должно быть подтверждено дословной цитатой из источников задачи.

    Цитаты из страниц документации сверяются с принятой копией (result/), а не с копией, которую правил агент.
    Возвращает список нарушений; пустой список - ответ подтвержден.
    """
    problems: List[str] = []
    sources = unit_sources(ds, u, kind)
    pages = {p: ds.result_dir / p[len("docs/"):] for p in unit_pages(ds, u)}

    def check(ev: Any, where: str) -> None:
        if not isinstance(ev, dict) or not ev.get("source") or not ev.get("quote"):
            problems.append(f"{where}: подтверждение должно быть объектом с полями source и quote")
            return
        src, quote = str(ev["source"]).strip("`"), _norm(str(ev["quote"]))
        if len(quote) < 3:
            problems.append(f"{where}: цитата слишком короткая")
            return
        if src in VIRTUAL_SOURCES:
            text = _virtual_text(u, src)
        elif src in sources:
            text = read_text(sources[src]) or ""
        elif src in pages:
            text = read_text(pages[src]) or ""
        else:
            problems.append(f"{where}: источник не входит в разрешенные для этой задачи: {src}")
            return
        if quote not in _norm(text):
            problems.append(f"{where}: цитаты нет в источнике {src}: «{quote[:80]}»")

    def check_list(evs: Any, where: str) -> None:
        if not isinstance(evs, list) or not evs:
            problems.append(f"{where}: нет подтверждения")
            return
        for n, ev in enumerate(evs, 1):
            check(ev, f"{where}, подтверждение {n}")

    if kind == "decide":
        check_list(answer.get("evidence"), "решение")
    edits = answer.get("edits")
    changed = answer.get("files_changed") or []
    if kind in ("write_doc", "check_doc", "check_langs") and (changed or edits):
        if not isinstance(edits, list) or not edits:
            problems.append("правки: файлы изменены, но нет списка edits с подтверждением каждой правки")
        else:
            for n, e in enumerate(edits, 1):
                if not isinstance(e, dict) or not e.get("file") or not e.get("what"):
                    problems.append(f"правка {n}: нужны поля file и what")
                    continue
                check_list(e.get("evidence"), f"правка {n} ({e['file']})")
    return problems


def check_answer(path: Path, item: Dict[str, Any], ds=None, unit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = read_text(path)
    if raw is None:
        return {"ok": False, "missing": True, "error": "ответа нет"}
    try:
        a = json.loads(raw)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"ответ не является JSON: {e}"}
    if not isinstance(a, dict):
        return {"ok": False, "error": "ответ должен быть объектом JSON"}
    miss = [k for k in ANSWER_REQUIRED if k not in a]
    if miss:
        return {"ok": False, "error": "в ответе нет полей: " + ", ".join(miss), "answer": a}
    if a["status"] not in STATUSES:
        return {"ok": False, "error": f"неизвестный status: {a['status']}", "answer": a}
    if ds is not None and str(a.get("rules", "")).strip() != rules_code(ds):
        return {"ok": False, "unverified": True, "answer": a,
                "problems": ["в ответе нет кода общих правил: агент не прочитал AGENTS.md в своей папке"],
                "error": "агент не прочитал общие правила (нет кода из AGENTS.md)"}
    if item["kind"] == "decide" and a["status"] == "done" and a.get("decision") not in ("doc", "rn", "langs", "screens", "skip"):
        return {"ok": False, "error": "для задания decide нужно поле decision: doc, rn, langs, screens или skip", "answer": a}
    if ds is not None and unit is not None and item["kind"] in NEED_EVIDENCE and a["status"] == "done":
        problems = verify_evidence(ds, unit, item["kind"], a)
        if problems:
            return {"ok": False, "unverified": True, "problems": problems, "answer": a,
                    "error": "не подтверждено источниками: " + "; ".join(problems[:4])
                             + (f"; и еще нарушений: {len(problems) - 4}" if len(problems) > 4 else "")}
    return {"ok": True, "answer": a}


def _declared_files(answer: Dict[str, Any]) -> List[str]:
    files = [str(x).strip("`") for x in (answer.get("files_changed") or [])]
    files += [str(e.get("file", "")).strip("`") for e in (answer.get("edits") or []) if isinstance(e, dict)]
    return sorted({f for f in files if f})


# ---------------------------------------------------------------- рабочая папка агента

def _doc_files(root: Path) -> Dict[str, Path]:
    out = {}
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.name != ".DS_Store":
                out[p.relative_to(root).as_posix()] = p
    return out


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def changed_in_workspace(ds) -> List[str]:
    """Файлы документации, которые агент изменил в своей рабочей папке с момента, когда пайплайн ее собрал.

    Сравнение идет со списком контрольных сумм, записанным при сборке папки, а не с result/: копию в result/
    пайплайн сам обновляет между запусками (например, пересобирает черновик release notes).
    """
    manifest_path = ds.agent_dir / "workspace_manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = load_json(manifest_path)
    w = _doc_files(workspace(ds) / "docs")
    return [f"docs/{k}" for k, p in w.items() if manifest.get(k) != _digest(p)]


def refresh_workspace(ds, items: List[Dict[str, Any]], prompts: Dict[str, str], units_by_id: Dict[str, Any]) -> None:
    """Собирает рабочую папку агента заново: принятая документация, задания, источники задач. Ответы сохраняются."""
    w = workspace(ds)
    for sub in ("docs", "tasks", "sources"):
        if (w / sub).exists():
            shutil.rmtree(w / sub)
    shutil.copytree(ds.result_dir, w / "docs", ignore=shutil.ignore_patterns(".DS_Store"))
    (w / "answers").mkdir(parents=True, exist_ok=True)
    for item in items:
        save_text(w / "tasks" / f"{item['id']}.md", prompts[item["id"]])
        if item["unit"]:
            for dst, src in unit_sources(ds, units_by_id[item["unit"]], item["kind"]).items():
                t = w / dst
                t.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, t)
    save_json(ds.agent_dir / "workspace_manifest.json", {k: _digest(p) for k, p in _doc_files(w / "docs").items()})
    order = "\n".join(f"{n}. `tasks/{i['id']}.md`" for n, i in enumerate(items, 1)) or "Заданий нет."
    text = rules_text(ds).replace("{{RULES_CODE}}", rules_code(ds)).replace("{{TASKS}}", order)
    save_text(w / "AGENTS.md", text)


def accept_edits(ds, results: Dict[str, Dict[str, Any]], allowed: Dict[str, List[str]]) -> Dict[str, Any]:
    """Переносит в result/ правки из подтвержденных ответов. Остальные изменения откладывает в agent/rejected/.

    allowed - какие страницы разрешено менять каждому заданию.
    """
    w = workspace(ds)
    changed = changed_in_workspace(ds)
    good: Dict[str, List[str]] = {}
    bad: Dict[str, List[str]] = {}
    foreign: Dict[str, List[str]] = {}
    for iid, r in results.items():
        a = r.get("answer") or {}
        for f in _declared_files(a):
            if f in LOG_FILES:
                continue
            if not r["ok"]:
                bad.setdefault(f, []).append(iid)
            elif f not in allowed.get(iid, []):
                foreign.setdefault(f, []).append(iid)
            else:
                good.setdefault(f, []).append(iid)
    any_ok = any(r["ok"] for r in results.values())
    accepted, rejected = [], []
    for f in changed:
        src = w / f
        if f in LOG_FILES:
            ok, why = any_ok, "журнал: нет ни одного принятого ответа"
        elif f in bad:
            ok, why = False, "файл назван в неподтвержденном ответе: " + ", ".join(bad[f])
        elif f in foreign:
            ok, why = False, "страница не входит в страницы задания: " + ", ".join(foreign[f])
        elif f in good:
            ok, why = True, ""
        else:
            ok, why = False, "файл изменен, но ни в одном ответе не назван"
        if ok:
            dst = ds.result_dir / f[len("docs/"):]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            accepted.append({"file": f, "by": good.get(f, [])})
        else:
            dst = ds.agent_dir / "rejected" / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            rejected.append({"file": f, "why": why})
    return {"accepted": accepted, "rejected": rejected}


# ---------------------------------------------------------------- запуск

def run_cmd(ds, item: Dict[str, Any], prompt: str, cmd: List[str], timeout: int) -> Optional[str]:
    """Запускает агента в его рабочей папке. Возвращает текст ошибки или None."""
    try:
        res = subprocess.run(cmd, input=prompt, text=True, capture_output=True, cwd=str(workspace(ds)), timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"агент не уложился в {timeout} с"
    except FileNotFoundError:
        return f"команда агента не найдена: {cmd[0]}"
    save_text(ds.agent_dir / "logs" / f"{item['id']}.log", (res.stdout or "") + ("\n--- stderr ---\n" + res.stderr if res.stderr else ""))
    if res.returncode != 0:
        return f"агент завершился с кодом {res.returncode}, см. logs/{item['id']}.log"
    return None


def run(ds, units: List[Dict[str, Any]], conf: Dict[str, Any], decisions: Dict[str, str], *, dry_run: bool,
        cmd: Optional[str], redo: bool, timeout: int, on_decided=None) -> Dict[str, Any]:
    """Сначала задания decide; их подтвержденные ответы дополняют решения, по ним строится остаток очереди."""
    by_id = {u["id"]: u for u in units}
    command = agent_cmd(cmd)
    launch = bool(command) and not dry_run
    w = workspace(ds)
    final = dict(decisions)
    results: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}

    def check(item) -> Dict[str, Any]:
        r = check_answer(w / "answers" / f"{item['id']}.json", item, ds, by_id.get(item["unit"]) if item["unit"] else None)
        results[item["id"]] = r
        return r

    def process(items: List[Dict[str, Any]]) -> None:
        prompts = {i["id"]: render_task(ds, i, by_id, conf, final) for i in items}
        if launch:
            earlier.append(sync_now())      # правки, сделанные до пересборки папки, не теряются
            refresh_workspace(ds, all_items + [i for i in items if i not in all_items], {**all_prompts, **prompts}, by_id)
        for n, item in enumerate(items, 1):
            all_prompts[item["id"]] = prompts[item["id"]]
            all_items.append(item)
            answer = w / "answers" / f"{item['id']}.json"
            if redo and launch and answer.exists():
                answer.unlink()
            r = check(item)
            if launch and not r["ok"]:
                log(f"агент: {item['id']} ({n}/{len(items)})")
                if answer.exists():
                    answer.unlink()
                err = run_cmd(ds, item, prompts[item["id"]], command, timeout)
                if err:
                    errors[item["id"]] = err
                r = check(item)
            if item["kind"] == "decide" and r["ok"] and r["answer"]["status"] == "done":
                final[item["unit"]] = r["answer"]["decision"]

    all_items: List[Dict[str, Any]] = []
    all_prompts: Dict[str, str] = {}
    earlier: List[Dict[str, Any]] = []
    rn_files = [f"docs/MD_{lang}/docs/{P.RELEASE_NOTES_DIR}/{ds.versions['release_notes_file']}" for lang in ds.langs]

    def sync_now() -> Dict[str, Any]:
        allowed = {i["id"]: (unit_pages(ds, by_id[i["unit"]]) if i["unit"] else rn_files) for i in all_items}
        return accept_edits(ds, results, allowed)

    process(build_queue(units, decisions, only_kinds=["decide"]))
    if on_decided:
        on_decided(final)
    process([i for i in build_queue(units, final) if i["kind"] != "decide"])

    sync = sync_now()
    for e in earlier:
        sync["accepted"] = e["accepted"] + sync["accepted"]
        sync["rejected"] = e["rejected"] + sync["rejected"]
    refresh_workspace(ds, all_items, all_prompts, by_id)

    for item in all_items:
        r = results[item["id"]]
        a = r.get("answer") or {}
        if item["id"] in errors and not r["ok"]:
            item.update(status="failed", note=errors[item["id"]])
        elif r["ok"]:
            item.update(status=a["status"], note=str(a.get("summary", ""))[:300])
        elif r.get("missing"):
            item.update(status="prepared", note="ответа нет")
        elif r.get("unverified"):
            item.update(status="unverified", note=r["error"])
        else:
            item.update(status="failed", note=r["error"])
        item["decision"] = a.get("decision") if item["kind"] == "decide" and r["ok"] else None
        item["problems"] = r.get("problems", [])
        item["log"] = [str(x) for x in (a.get("log") or [])][:20]
        item["declared_files"] = _declared_files(a)
        item["questions"] = [str(x) for x in (a.get("questions") or [])]
    save_json(ds.agent_dir / "queue.json", {"command": command, "launched": launch, "items": all_items, **sync})
    save_json(ds.agent_dir / "final_decisions.json", final)
    return {"items": all_items, "final": final, "launched": launch, "command": command, **sync}


def summary_md(ds, res: Dict[str, Any], decisions: Dict[str, str]) -> str:
    w = ds.rel(workspace(ds))
    counts: Dict[str, int] = {}
    for i in res["items"]:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    names = {"done": "принято", "needs_human": "нужен человек", "unverified": "не подтверждено",
             "failed": "ошибка", "prepared": "ждет агента"}
    L = [f"# Журнал работы агента: набор данных {ds.name}", "",
         "Состояние заданий: " + (", ".join(f"{names[k]} - {v}" for k, v in sorted(counts.items())) or "заданий нет") + ".", ""]
    if not res["launched"]:
        L += [f"Агент запускается вручную. Откройте в редакторе только папку `{w}` и дайте агенту задания из "
              "`tasks/` по порядку из файла `AGENTS.md`. После этого запустите команду `agent` еще раз: она проверит "
              "ответы, перенесет принятые правки и обновит этот журнал.", ""]
    L += ["| Задание | Состояние | Решение | Итог |", "| --- | --- | --- | --- |"]
    for i in res["items"]:
        L.append(f"| {i['id']} | {names[i['status']]} | {i.get('decision') or '-'} | {(i.get('note') or '-').replace('|', '/')} |")
    if res["accepted"]:
        L += ["", "## Принятые правки", "", "Перенесены в `result/`.", ""]
        L += [f"- `{a['file']}`" + (f": {', '.join(a['by'])}" if a["by"] else "") for a in res["accepted"]]
    if res["rejected"]:
        L += ["", "## Отклоненные правки", "",
              "В `result/` не перенесены. Копии лежат в `agent/rejected/`, в рабочей папке агента файлы возвращены "
              "к принятому состоянию.", ""]
        L += [f"- `{r['file']}`: {r['why']}" for r in res["rejected"]]
    detail = [i for i in res["items"] if i["problems"] or i["log"] or i["questions"] or i["status"] == "failed"]
    if detail:
        L += ["", "## Подробности по заданиям", ""]
    for i in detail:
        L += [f"### {i['id']}: {names[i['status']]}", ""]
        if i["problems"]:
            L += ["Что не подтверждено:", ""] + [f"- {p}" for p in i["problems"]] + [""]
        if i["questions"]:
            L += ["Вопросы агента:", ""] + [f"- {q}" for q in i["questions"]] + [""]
        if i["log"]:
            L += ["Ход работы (со слов агента):", ""] + [f"- {x}" for x in i["log"]] + [""]
        if i["declared_files"]:
            L += ["Файлы, которые агент назвал измененными: " + ", ".join(f"`{f}`" for f in i["declared_files"]), ""]
    changed = {k: v for k, v in res["final"].items() if decisions.get(k) != v}
    if changed:
        L += ["", "## Решения агента", ""] + [f"- {k}: {V.DECISION_TEXT[v]}" for k, v in changed.items()]
    left = [k for k, v in res["final"].items() if v == "agent"]
    if left:
        L += ["", "Остались без решения: " + ", ".join(left) + "."]
    return "\n".join(L) + "\n"
