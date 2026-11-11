"""Человекочитаемый отчёт по шагам 1–2 (report.md)."""

from __future__ import annotations

from typing import Any, Dict, List

ACTION_RU = {
    "doc_and_rn": "в доку + RN",
    "rn_and_check_doc": "RN + проверить доку",
    "rn_and_check_langs": "RN + проверить список языков",
    "screenshot_check": "проверить скрины",
    "review": "нужна проверка",
    "skip": "отброшено",
}


def _cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def build_report(cfg, changelog, code, reach_info, i18n_total, jira, units, unattributed, admin_units) -> str:
    L: List[str] = []
    L.append(f"# Изменения для релиза {cfg.release}\n")
    L.append(f"point-ui {changelog['lower']} → {changelog['upper']}. Версии ченджлога: "
             + ", ".join(f"{v['version']} ({v['count']})" for v in changelog["versions"]) + ".\n")
    acts: Dict[str, int] = {}
    for u in units:
        a = u["decision"]["action"]["value"]
        acts[a] = acts.get(a, 0) + 1
    L.append("## Итог\n")
    L.append(f"- Строк ченджлога: {len(changelog['entries'])}, единиц (задач): {len(units)}.")
    L.append("- Решения: " + ", ".join(f"{ACTION_RU.get(k, k)} — {v}" for k, v in sorted(acts.items(), key=lambda kv: -kv[1])) + ".")
    L.append(f"- Изменённых файлов кода: {code['changed_count']} ({', '.join(f'{k}: {v}' for k, v in sorted(code['by_category'].items()))}).")
    L.append(f"- Файлов, достижимых из приложения: {reach_info['count']}; неразрешённых внутренних импортов: {reach_info['unresolved']}.")
    for loc, d in i18n_total["locales"].items():
        L.append(f"- Строки UI [{loc}]: +{len(d['added'])} / −{len(d['removed'])}.")
    L.append(f"- Задач Jira в выгрузке: {len(jira)}." + (" Выгрузки нет — правила В1/В2 не применялись." if not jira else ""))
    L.append("")

    L.append("## Единицы изменения\n")
    L.append("| Задача | Продукт | Видимость | Класс | Решение | Разделы доки |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    order = {"doc_and_rn": 0, "rn_and_check_doc": 1, "rn_and_check_langs": 2, "review": 3, "screenshot_check": 4, "skip": 5}
    for u in sorted(units, key=lambda u: (order.get(u["decision"]["action"]["value"], 9), u["id"])):
        d = u["decision"]
        docs = ", ".join(c["doc"].split("/")[-1] for c in u["doc_candidates"][:4])
        L.append(f"| {u['id']} | {d['product']['value']} ({d['product']['rule']}) | {d['visibility']['value']} ({d['visibility']['rule']}) "
                 f"| {d['class_draft']['value']} | {ACTION_RU.get(d['action']['value'], d['action']['value'])} | {_cell(docs)} |")
    L.append("")

    L.append("## Подробно\n")
    for u in units:
        d = u["decision"]
        L.append(f"### {u['id']}" + (f" — {_cell(u['jira']['summary'])}" if u.get("jira") else ""))
        L.append(f"- Продукт: **{d['product']['value']}** по {d['product']['rule']}: {'; '.join(d['product']['reasons'])}")
        L.append(f"- Видимость: **{d['visibility']['value']}** по {d['visibility']['rule']}: {'; '.join(d['visibility']['reasons'])}")
        L.append(f"- Класс (черновик): {d['class_draft']['value']} — {d['class_draft']['why']}")
        L.append(f"- Решение: **{ACTION_RU.get(d['action']['value'], d['action']['value'])}** — {d['action']['why']}")
        L.append("- Строки ченджлога:")
        for e in u["entries"]:
            mk = f" · метки: {', '.join(e['_markers'])}" if e["_markers"] else ""
            fl = e["_flags"]
            if not fl.get("significant", True):
                why = "другой продукт" if fl["other_product"] else "тест" if fl["test"] else "техническая"
                mk += f" · не учитывается: {why}"
            L.append(f"    - `{e['version']}` {e['section']} [{']['.join(e['tags'])}] {_cell(e['text'])[:300]}{mk}")
        if u["files"]:
            n_rel = sum(1 for f in u["files"] if f.get("relevant"))
            L.append(f"- Файлы ({len(u['files'])}, из них по значимым записям: {n_rel}): " + ", ".join(
                f"`{f['path']}`{'' if f['reachable_from_app'] else ' (не в point)'}" for f in u["files"][:12])
                + (" …" if len(u["files"]) > 12 else ""))
        ch = u["i18n"]
        if ch["added"] or ch["removed"] or ch["changed"]:
            L.append(f"- Строки UI: +{len(ch['added'])} / −{len(ch['removed'])} / изменено {len(ch['changed'])}")
            for c in ch["changed"][:6]:
                L.append(f"    - [{c['locale']}] «{_cell(c['old'])}» → «{_cell(c['new'])}»")
        if ch.get("other_product"):
            L.append(f"- Строки UI только для другого продукта (не учитываются): {len(ch['other_product'])}")
        if u.get("doc_mentions"):
            L.append("- Старые тексты встречаются в базовой доке:")
            for lang, hits in u["doc_mentions"].items():
                for needle, found in list(hits.items())[:5]:
                    L.append(f"    - [{lang}] «{_cell(needle)}»: " + ", ".join(f"{h['file']}:{h['line']}" for h in found[:5]))
        if u["doc_candidates"]:
            L.append("- Кандидаты разделов: " + ", ".join(f"`{c['doc']}`" for c in u["doc_candidates"]))
        L.append("")

    if admin_units:
        L.append("## Версии компонентов (Д10)\n")
        for a in admin_units:
            L.append(f"- `{a['variable']}`: {a['old']} → {a['new']}")
            for lang, hits in a["doc_mentions"].items():
                for h in hits:
                    L.append(f"    - [{lang}] {h['file']}:{h['line']} — `{_cell(h['text'])}` — {h.get('note', '')}")
        L.append("")

    if unattributed:
        L.append(f"## Файлы без привязки к записям ченджлога ({len(unattributed)})\n")
        L.append("Изменены в коде, но ни одна запись на них не указывает. Агент проверяет их на шаге 3.\n")
        for f in unattributed[:80]:
            L.append(f"- `{f['path']}` · {f['status']} · {f['category']}")
        if len(unattributed) > 80:
            L.append(f"- … ещё {len(unattributed) - 80}")
    return "\n".join(L) + "\n"
