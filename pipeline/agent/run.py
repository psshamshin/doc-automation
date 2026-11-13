"""Запуск агента: рабочая папка, очередь заданий, повторная попытка, журнал.

Порядок для каждого задания:
1. агент отвечает файлом в answers/;
2. код проверяет ответ; прошел - правки внесены;
3. не прошел - готовится повторное задание с причинами (одна попытка); снова не прошел - ничего не вносится.
Человек в этом не участвует: он смотрит только первичный review.md и итоговый журнал.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import draft as DRAFT
from .. import product as P
from .. import validation as V
from ..common import ROOT, load_json, log, read_text, save_json, save_text
from .tasks import (PROMPTS, STALE_ID, agent_cmd, build_queue, render_task, stale_unit, unit_dir, unit_sources,
                    workspace, index_of)
from .verify import Docs, apply_ops, check_answer, verify_answer, verify_rn

NAMES = {"done": "принято", "unknown": "агент не нашел фактов", "rejected": "отклонено, нужна повторная попытка",
         "partial": "принято частично, часть правок отклонена", "dropped": "не внесено: отклонено дважды", "prepared": "ждет агента", "waiting": "ждет решений по задачам"}


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def refresh_workspace(ds, recs: List[Dict[str, Any]], prompts: Dict[str, str], units_by_id: Dict[str, Any]) -> None:
    """Собирает папку агента заново: документация, задания, источники. Ответы и progress.md сохраняются."""
    w = workspace(ds)
    for sub in ("docs", "tasks", "sources"):
        if (w / sub).exists():
            shutil.rmtree(w / sub)
    shutil.copytree(ds.result_dir, w / "docs", ignore=shutil.ignore_patterns(".DS_Store"))
    (w / "answers").mkdir(parents=True, exist_ok=True)
    if not (w / "progress.md").exists():
        save_text(w / "progress.md", "# Ход работы агента\n\nАгент дописывает сюда строку в начале и в конце каждого задания и на каждом шаге.\n\n")
    for r in recs:
        save_text(w / "tasks" / f"{r['id']}.md", prompts[r["id"]])
        if r["unit"] and r["unit"] in units_by_id:
            for dst, src in unit_sources(ds, units_by_id[r["unit"]]).items():
                (w / dst).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, w / dst)
    todo = "\n".join(f"{n}. `tasks/{r['id']}.md`" for n, r in enumerate([r for r in recs if r["status"] == "prepared"], 1)) \
        or "Заданий, ждущих ответа, нет."
    save_text(w / "AGENTS.md", (read_text(PROMPTS / "workspace_agents.md") or "")
              .replace("{{RELEASE}}", ds.versions["release"]).replace("{{TASKS}}", todo))


def run_cmd(ds, rec: Dict[str, Any], prompt: str, cmd: List[str], timeout: int) -> Optional[str]:
    """Запускает агента в его папке. Возвращает текст ошибки или None."""
    try:
        res = subprocess.run(cmd, input=prompt, text=True, capture_output=True, cwd=str(workspace(ds)), timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"агент не уложился в {timeout} с"
    except FileNotFoundError:
        return f"команда агента не найдена: {cmd[0]}"
    save_text(ds.agent_dir / "logs" / f"{rec['id']}.log", (res.stdout or "") + ("\n--- stderr ---\n" + res.stderr if res.stderr else ""))
    return f"агент завершился с кодом {res.returncode}, см. logs/{rec['id']}.log" if res.returncode else None


def run(ds, units: List[Dict[str, Any]], conf: Dict[str, Any], decisions: Dict[str, str], index: Dict[str, Any],
        stale: List[Dict[str, Any]], *, dry_run: bool, cmd: Optional[str], redo: bool, timeout: int) -> Dict[str, Any]:
    """Готовит задания, проверяет ответы, вносит принятые правки. Принятый ответ повторно не вносится."""
    by_id = {u["id"]: u for u in units}
    if stale_unit(ds, stale):
        by_id[STALE_ID] = stale_unit(ds, stale)
    command = agent_cmd(cmd)
    launch = bool(command) and not dry_run
    w = workspace(ds)
    qpath = ds.agent_dir / "queue.json"
    prev_q = load_json(qpath) if qpath.is_file() else {}
    prev = {i["id"]: i for i in prev_q.get("items", [])}
    docs, final = Docs(ds), dict(decisions)
    queue = build_queue(units, decisions, bool(stale))
    recs: List[Dict[str, Any]] = []
    prompts: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    seconds: Dict[str, int] = {}

    def new_rec(base: Dict[str, Any], **extra) -> Dict[str, Any]:
        r = {"id": base["id"], "unit": base["unit"], "kind": base["kind"], "status": "prepared", "note": "", "problems": [],
             "applied": [], "decision": None, "steps": [], "questions": [], "summary": "", "approx": [], "answer_sha": None}
        r.update(extra)
        prompts[r["id"]] = render_task(ds, r, by_id, conf, final)
        return r

    def answer_of(rec) -> Path:
        return w / "answers" / f"{rec['id']}.json"

    def obtain(rec) -> None:
        """В режиме запуска агент вызывается, если ответа еще нет."""
        path = answer_of(rec)
        if redo and path.exists():
            path.unlink()
        if launch and not path.exists():
            log(f"агент: {rec['id']}")
            t0 = time.time()
            err = run_cmd(ds, rec, prompts[rec["id"]], command, timeout)
            seconds[rec["id"]] = int(time.time() - t0)
            if err:
                errors[rec["id"]] = err

    def finish(rec, check: Dict[str, Any], verified: Optional[Dict[str, Any]] = None) -> None:
        a = check.get("answer") or {}
        rec.update(summary=str(a.get("summary", ""))[:300], questions=[str(x) for x in (a.get("questions") or [])][:8],
                   steps=[f"{s.get('source')}: {s.get('found')}" for s in (a.get("steps") or []) if isinstance(s, dict)][:30])

    def evaluate(rec, base: Optional[Dict[str, Any]] = None) -> None:
        """Определяет состояние задания по файлу ответа. Задание release_notes проверяется отдельно."""
        path = answer_of(rec)
        old = prev.get(rec["id"], {})
        if rec["kind"] != "release_notes" and path.is_file() and old.get("answer_sha") == _digest(path) \
                and old.get("status") in ("done", "unknown", "dropped", "partial") and not redo:
            rec.update({k: old[k] for k in ("status", "note", "problems", "applied", "decision", "steps", "questions",
                                            "summary", "approx", "seconds") if k in old})
            rec["answer_sha"] = old["answer_sha"]
            return
        obtain(rec)
        chk = check_answer(path, rec)
        rec["answer_sha"] = _digest(path) if path.is_file() else None
        if chk.get("missing"):
            if rec["id"] in errors:
                rec.update(status="rejected", problems=[errors[rec["id"]]], note=errors[rec["id"]])
            return
        if not chk["ok"]:
            rec.update(status="rejected", problems=[chk["error"]], note=chk["error"])
            return
        finish(rec, chk)
        a = chk["answer"]
        if rec["kind"] == "release_notes":
            rec["_answer"] = a
            return
        if a["status"] == "unknown" and not a.get("edits"):
            rec.update(status="unknown", note=rec["summary"])
            return
        v = verify_answer(ds, by_id[rec["unit"]], rec["kind"], a, docs, conf, rec["id"])
        ops = apply_ops(ds, docs, rec["unit"], v) if v["ops"] else []
        rec.update(applied=[{"file": o["file"], "what": o["what"]} for o in ops], approx=v["approx"], notes=v["notes"][:10])
        if rec["kind"] == "decide" and not v["decision_problems"] and a["status"] == "done":
            rec["decision"] = a.get("decision")
        if v["problems"]:
            rec.update(status="partial" if ops else "rejected", problems=v["problems"],
                       note="; ".join(v["problems"][:2]) + (f"; и еще: {len(v['problems']) - 2}" if len(v["problems"]) > 2 else ""))
        else:
            rec.update(status=a["status"], note=rec["summary"])

    def attempt(base: Dict[str, Any], rn: bool = False) -> List[Dict[str, Any]]:
        """Первая попытка и, если ответ отклонен, одна повторная. Возвращает записи заданий."""
        r1 = new_rec(base)
        (evaluate_rn if rn else evaluate)(r1)
        out = [r1]
        if r1["status"] in ("rejected", "partial"):
            r2 = new_rec({**base, "id": base["id"] + "__retry"}, retry_of=base["id"], problems_before=r1["problems"])
            (evaluate_rn if rn else evaluate)(r2)
            if r2["status"] == "rejected":
                r2["status"] = "dropped"
            if r2["status"] == "partial" and r1["status"] == "partial":
                pass
            out.append(r2)
        return out

    def leaf(rs: List[Dict[str, Any]]) -> Dict[str, Any]:
        return rs[-1]

    if launch:                                    # агент запускается в своей папке: она должна быть собрана до запуска
        refresh_workspace(ds, [new_rec(b) for b in queue], prompts, by_id)

    # --- решения и правки по задачам, затем устаревшие названия
    pending_decide = False
    for base in [b for b in queue if b["kind"] != "release_notes"]:
        rs = attempt(base)
        recs += rs
        last = leaf(rs)
        if base["kind"] == "decide":
            if last["status"] == "prepared":
                pending_decide = True
            elif last["status"] == "done" and last.get("decision"):
                final[base["unit"]] = last["decision"]

    # --- release notes: после решений по всем спорным задачам
    rn_lines: Dict[str, Dict[str, str]] = {}
    rn_skipped: Dict[str, str] = {}

    def evaluate_rn(rec) -> None:
        """Строки принимаются по задачам; принятые копятся между попытками, release notes собираются из них."""
        if pending_decide:
            rec["status"] = "waiting"
            return
        evaluate(rec)
        a = rec.pop("_answer", None)
        if rec["status"] == "rejected" or a is None:
            return
        expected = [u["id"] for u in units if final.get(u["id"]) in ("doc", "rn", "langs")
                    and u["id"] not in rn_lines and u["id"] not in rn_skipped]
        v = verify_rn(a, expected)
        rn_lines.update(v["lines"])
        rn_skipped.update(v["skipped"])
        DRAFT.write_release_notes(ds, units, final, index, rn_lines)
        what = "файлы RU и EN собраны кодом"
        if rn_skipped:
            what += "; без строки по решению агента: " + "; ".join(f"{u} ({why[:80]})" for u, why in rn_skipped.items())
        rec.update(summary=rec["summary"], applied=[{"file": f"release notes: строк {len(rn_lines)}", "what": what}])
        if v["problems"]:
            probs = [f"{u}: {x}" for u, ps in v["problems"].items() for x in ps]
            rec.update(status="partial", problems=probs, note="; ".join(probs[:2]))
        else:
            rec.update(status="done", note=rec["summary"])

    for base in [b for b in queue if b["kind"] == "release_notes"]:
        recs += attempt(base, rn=True)

    for r in recs:
        if r["id"] in seconds:
            r["seconds"] = seconds[r["id"]]
    for r in recs:
        r.setdefault("seconds", None)

    # --- итог
    for r in recs:
        prompts[r["id"]] = render_task(ds, r, by_id, conf, final)
    refresh_workspace(ds, recs, prompts, by_id)
    ds.agent_dir.mkdir(parents=True, exist_ok=True)
    if (w / "progress.md").is_file():
        shutil.copyfile(w / "progress.md", ds.agent_dir / "progress.md")
    save_json(qpath, {"command": command, "launched": launch, "built_at": time.time(), "items": recs})
    save_json(ds.agent_dir / "final_decisions.json", final)
    return {"items": recs, "final": final, "launched": launch, "command": command}


def summary_md(ds, res: Dict[str, Any], decisions: Dict[str, str]) -> str:
    recs = res["items"]
    counts: Dict[str, int] = {}
    for r in recs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    L = [f"# Журнал работы агента: {ds.name}", "",
         "Состояние: " + (", ".join(f"{NAMES[k]} - {v}" for k, v in sorted(counts.items())) or "заданий нет") + ".", ""]
    waiting = [r["id"] for r in recs if r["status"] == "prepared"]
    if waiting and not res["launched"]:
        L += [f"Откройте в редакторе только папку `{ds.rel(workspace(ds))}` и дайте агенту задания из `tasks/` по порядку "
              "из `AGENTS.md`, затем запустите `agent` еще раз. Ждут ответа: " + ", ".join(waiting) + ".", ""]
    total = sum(r.get("seconds") or 0 for r in recs)
    if total:
        L += [f"Время агента: {total // 60} мин {total % 60} с. Ход работы: `agent/progress.md`.", ""]
    L += ["| Задание | Состояние | Решение | Правок | Время | Итог |", "| --- | --- | --- | --- | --- | --- |"]
    for r in recs:
        sec = r.get("seconds")
        t = f"{sec // 60}:{sec % 60:02d}" if sec is not None else "-"
        L.append(f"| {r['id']} | {NAMES[r['status']]} | {r.get('decision') or '-'} | {len(r['applied'])} | {t} | "
                 f"{(r.get('note') or '-').replace('|', '/')[:200]} |")
    detail = [r for r in recs if r["status"] not in ("prepared", "waiting")]
    if detail:
        L += ["", "## Подробности", ""]
    for r in detail:
        L += [f"### {r['id']}: {NAMES[r['status']]}", ""]
        if r["summary"]:
            L += [f"Итог агента: {r['summary']}", ""]
        if r["problems"]:
            L += ["Причины отклонения:", ""] + [f"- {p}" for p in r["problems"]] + [""]
        if r["applied"]:
            L += ["Внесено кодом:", ""] + [f"- `{a['file']}`: {a['what']}" for a in r["applied"]] + [""]
        if r["approx"]:
            L += [f"Цитат принято с поправкой на опечатку: {len(r['approx'])}.", ""]
        if r["questions"]:
            L += ["Замечания агента (ответа не требуют):", ""] + [f"- {q}" for q in r["questions"]] + [""]
        if r["steps"]:
            L += ["Шаги агента:", ""] + [f"- {x}" for x in r["steps"]] + [""]
    changed = {k: v for k, v in res["final"].items() if decisions.get(k) != v}
    if changed:
        L += ["", "## Решения агента", ""] + [f"- {k}: {V.DECISION_TEXT[v]}" for k, v in changed.items()]
    return "\n".join(L) + "\n"
