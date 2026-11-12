"""Шаг 1 (сборка единиц изменения) и шаг 2 (детерминированная классификация).

Правила и история их изменений: docs/RULES.md.

Единица = задача (GUI-xxxx). Для каждой: строки ченджлога, привязанные файлы кода,
изменения строк интерфейса, задача Jira, решения по правилам с причинами,
кандидаты разделов доки и упоминания старых текстов в базовой доке.

Решения детерминированные и объяснимые. Где правил не хватает — 'needs_review'
и вопрос для агента (шаг 3), а не догадка.
"""

from __future__ import annotations

import re
from collections import OrderedDict, defaultdict
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------- привязка файлов к записям


def _segments(path: str) -> List[str]:
    return path.split("/")


def files_for_entry(entry: Dict[str, Any], changed: List[Dict[str, Any]], rules: Dict[str, Any]) -> Dict[str, List[str]]:
    """Файлы из диффа, на которые указывает запись ченджлога. Возвращает {path: [почему]}."""
    pkg_dirs = set(rules["attribution"]["package_dirs"])
    app_dir = rules["attribution"]["app_dir"]
    tag_alias = rules["attribution"].get("tag_aliases", {})
    tags: List[str] = []
    for t in entry["tags"]:
        t = tag_alias.get(t, t)
        # алиас вида 'pages/video-analytics' → пакет + подпуть
        if not tags and "/" in t and t.split("/", 1)[0] in pkg_dirs:
            tags += t.split("/", 1)
        else:
            tags.append(t)
    hits: Dict[str, List[str]] = defaultdict(list)

    prefixes: List[str] = []
    if tags and tags[0] in pkg_dirs:
        base = f"packages/{tags[0]}"
        subs = [t for t in tags[1:] if t not in rules["attribution"]["ignore_tags"]]
        # тег только с именем пакета ([pages]) слишком широкий — привязываем лишь по именам в `…`
        for s in subs:
            prefixes += [f"{base}/{s}", f"{base}/src/{s}"]
    elif tags:
        for t in tags:
            if t in rules["attribution"]["ignore_tags"]:
                continue
            prefixes += [f"{app_dir}/src/app/{t}", f"{app_dir}/src/{t}", f"{app_dir}/src/components/{t}"]

    for f in changed:
        p = f["path"]
        for pref in prefixes:
            if p == pref or p.startswith(pref.rstrip("/") + "/") or p.startswith(pref + "."):
                hits[p].append(f"тег → {pref}")
                break

    # запасной вариант: тег короче реального пути ([monokit][page-header] → monokit/widgets/page-header)
    if not hits and tags and tags[0] in pkg_dirs:
        base = f"packages/{tags[0]}/"
        for s in [t for t in tags[1:] if t not in rules["attribution"]["ignore_tags"]]:
            last = s.rstrip("/").split("/")[-1]
            if len(last) < 4:
                continue
            for f in changed:
                p = f["path"]
                if p.startswith(base) and (f"/{s}/" in p or f"/{last}/" in p or f"/{last}." in p):
                    hits[p].append(f"сегмент пути → {last}")

    # имена компонентов/хуков: в обратных кавычках, CamelCase в тексте, kebab-тег → CamelCase
    refs = list(entry.get("code_refs", []))
    refs += re.findall(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b", entry["text"])
    for t in tags[1:]:
        last = t.rstrip("/").split("/")[-1]
        if "-" in last:
            refs.append("".join(w.capitalize() for w in last.split("-")))
    for ref in dict.fromkeys(refs):
        ref = ref.strip()
        if not re.fullmatch(r"[A-Za-z_][\w.\-]{3,}", ref):
            continue
        stem = ref.split("/")[-1]
        for f in changed:
            name = f["path"].rsplit("/", 1)[-1]
            if name.split(".")[0] == stem.split(".")[0] and (stem in name or name.startswith(stem)):
                hits[f["path"]].append(f"имя `{ref}`")
    return hits


# ---------------------------------------------------------------- маркеры текста

def _markers(entry: Dict[str, Any], rules: Dict[str, Any]) -> Set[str]:
    out = set()
    text = entry["text"] + " " + " ".join(entry["tags"])
    for prod, cfg in rules["product"]["text_markers"].items():
        if any(t in entry["tags"] for t in cfg.get("tags", [])):
            out.add(prod)
            continue
        if any(re.search(rx, text, flags=re.I) for rx in cfg.get("regex", [])):
            if not any(re.search(rx, text, flags=re.I) for rx in cfg.get("unless_regex", [])):
                out.add(prod)
    return out


def _kw(text: str, words: List[str]) -> List[str]:
    t = text.lower()
    return [w for w in words if w.lower() in t]


# ---------------------------------------------------------------- классификация

def _has_code_ref(text: str, rules) -> bool:
    return any(re.search(rx, text) for rx in rules["visibility"].get("code_reference_regex", []))


def entry_flags(e: Dict[str, Any], rules) -> Dict[str, bool]:
    """Признаки одной записи: другой продукт, тест, техническая, есть слова интерфейса, значимая.

    Техническая запись (изменение 2, GUI-4057, GUI-4134):
    * есть сильное техническое слово (перенос или перевод кода) и ссылка на код - всегда техническая,
      слова интерфейса в такой записи описывают место кода, а не изменение на экране;
    * есть обычное техническое слово и нет слов интерфейса.
    Значимая запись (изменение 1): без меток другого продукта, не тестовая, не техническая.
    """
    vis = rules["visibility"]
    ui = bool(_kw(e["text"], vis["ui_keywords"]))
    tech = bool(_kw(e["text"], vis["technical_keywords"]))
    strong = bool(_kw(e["text"], vis.get("technical_strong_keywords", []))) and _has_code_ref(e["text"], rules)
    test = bool(set(e["tags"]) & set(vis["test_tags"]))
    other = bool(e["_markers"])
    technical = strong or (tech and not ui)
    return {"other_product": other, "test": test, "technical": technical, "ui": ui,
            "significant": not other and not test and not technical}


def is_other_product_path(path: str, rules) -> bool:
    """Файл другого продукта: метка в имени ('.point3.') или папка другого приложения (изменение 7)."""
    prod = rules["product"]
    return any(m in path for m in prod["other_product_file_markers"]) or \
        any(path.startswith(d + "/") for d in prod["other_app_dirs"])


def significant_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in entries if e["_flags"]["significant"]]


def decide_product(unit, jira_issue, entry_files: Dict[str, Set[str]], reachable: Set[str], rules) -> Dict[str, Any]:
    """Порядок правил: В1 (Jira: POINT 2), затем В7, В3-В6, В9 по коду, В2 (Jira: другой продукт и кода POINT 2 нет).
    Файлы считаются только по записям без меток другого продукта: общие barrel-экспорты делают
    «достижимыми» и компоненты point3."""
    app_dir = rules["attribution"]["app_dir"]
    point_tag = rules["product"]["jira_point_tag"]
    jira_other = jira_product(jira_issue, rules)
    if jira_other == "point2":
        return {"value": "point2", "rule": "В1", "reasons": [f"Jira: префикс [{point_tag}] в названии"]}
    code = _product_by_code(unit, entry_files, reachable, rules)
    if jira_other is None:
        return code
    # Jira относит задачу к другому продукту (изменение 16)
    if code["value"] == "point2":
        code["jira_conflict"] = jira_other
        code["reasons"] = code["reasons"] + [f"в Jira задача отнесена к другому продукту ({jira_other}), но ее код использует POINT 2"]
        return code
    return {"value": "other", "rule": "В2", "reasons": [f"Jira: задача отнесена к другому продукту ({jira_other}), "
                                                         "кода POINT 2 у значимых записей нет"]}


def jira_product(jira_issue, rules) -> Optional[str]:
    """Что говорит о продукте название задачи в Jira: 'point2', имя другого продукта или None (ничего).

    Метки, не относящиеся к продуктам (например, [shared]), ни о чем не говорят. Без меток учитывается
    упоминание другого продукта в тексте названия ('... в Point 3').
    """
    if not jira_issue:
        return None
    prod = rules["product"]
    tags = jira_issue.get("product_tags") or []
    if prod["jira_point_tag"] in tags:
        return "point2"
    other = [t for t in tags if t in prod.get("jira_other_tags", [])]
    if other:
        return ", ".join(other)
    summary = jira_issue.get("summary") or ""
    for name, cfg in prod["text_markers"].items():
        if any(re.search(rx, summary, flags=re.I) for rx in cfg.get("regex", [])) and \
                not any(re.search(rx, summary, flags=re.I) for rx in cfg.get("unless_regex", [])):
            return name
    return None


def _product_by_code(unit, entry_files: Dict[str, Set[str]], reachable: Set[str], rules) -> Dict[str, Any]:
    """Правила В3-В9: продукт по записям списка изменений и файлам кода."""
    app_dir = rules["attribution"]["app_dir"]
    entries = unit["entries"]
    unmarked = [e for e in entries if not e["_flags"]["other_product"]]
    paths_all = sorted({p for s in entry_files.values() for p in s})
    paths = sorted({p for e in unmarked for p in entry_files.get(e["id"], set())})
    other_apps = rules["product"]["other_app_dirs"]
    suffix_other = rules["product"]["other_product_file_markers"]

    if not unmarked:
        prods = sorted({m for e in entries for m in e["_markers"]})
        return {"value": "other", "rule": "В7", "reasons": [f"все строки помечены как другой продукт: {prods}"]}
    if paths and all(p.startswith(app_dir + "/") for p in paths):
        return {"value": "point2", "rule": "В3", "reasons": ["все файлы в " + app_dir]}
    if paths and all(any(p.startswith(o + "/") for o in other_apps) for p in paths):
        return {"value": "other", "rule": "В4", "reasons": ["все файлы в приложениях других продуктов"]}
    point_files = [p for p in paths if p in reachable and not any(m in p for m in suffix_other)]
    if point_files:
        r = [f"{len(point_files)} файл(ов) из записей без меток достижимы из {app_dir}"]
        if len(unmarked) < len(entries):
            r.append(f"{len(entries) - len(unmarked)} строк про другой продукт не учитывались")
        return {"value": "point2", "rule": "В5", "reasons": r}
    if paths:
        return {"value": "other", "rule": "В6", "reasons": ["ни один файл записей без меток не достижим из " + app_dir]}
    if paths_all:
        return {"value": "needs_review", "rule": "В9", "reasons": ["файлы есть только у строк с метками другого продукта"]}
    return {"value": "needs_review", "rule": "В9", "reasons": ["файлы не привязаны, меток нет — нужна проверка"]}


def decide_visibility(sig_entries, files, i18n, rules) -> Dict[str, Any]:
    """Видимость по значимым записям и файлам, привязанным только к ним (изменение 1)."""
    vis = rules["visibility"]
    cats = {f["category"] for f in files}
    if not sig_entries:
        return {"value": "not_visible", "rule": "Г4",
                "reasons": ["нет значимых записей: только другой продукт, тесты и технические записи"]}
    if i18n["added"] or i18n["removed"] or i18n["changed"]:
        return {"value": "visible", "rule": "Г1", "reasons": ["изменены строки интерфейса в файлах значимых записей"]}
    if any("/navigation/" in f["path"] or "/routes" in f["path"] or "/permissions/" in f["path"] for f in files):
        return {"value": "visible", "rule": "Г2", "reasons": ["изменены навигация, роуты или права"]}
    if files and cats <= set(vis["invisible_categories"]):
        return {"value": "not_visible", "rule": "Г4", "reasons": [f"только файлы категорий {sorted(cats)}"]}
    if files and cats <= {"style"} | set(vis["invisible_categories"]):
        return {"value": "style_only", "rule": "Г3", "reasons": ["только стили: в доку не идет, возможна пересъемка скрина"]}
    # Г3 по тексту (изменение 8): все значимые записи - исправления оформления
    style_words = vis.get("style_keywords", [])
    if style_words and all(e["type"] == "fix" and _kw(e["text"], style_words) for e in sig_entries):
        words = sorted({w for e in sig_entries for w in _kw(e["text"], style_words)})
        return {"value": "style_only", "rule": "Г3",
                "reasons": ["все значимые записи - исправления оформления (" + ", ".join(words) + "), тексты интерфейса не менялись"]}
    ui = [e for e in sig_entries if e["_flags"]["ui"]]
    if ui:
        words = sorted({w for e in ui for w in _kw(e["text"], vis["ui_keywords"])})
        fix = all(e["type"] == "fix" for e in ui)
        return {"value": "visible", "rule": "Г5" if fix else "Г2", "reasons": ["в тексте: " + ", ".join(words[:6])]}
    return {"value": "unknown", "rule": "—", "reasons": ["недостаточно сигналов"]}


def decide_class(unit, files, i18n, locale_added: List[str], rules) -> Dict[str, Any]:
    """Черновой класс по значимым записям; если их нет - по всем записям задачи (единица все равно отбрасывается)."""
    rel = significant_entries(unit["entries"]) or unit["entries"]
    cls = rules.get("class", {})
    types = [e["type"] for e in rel]
    texts = " ".join(e["text"] for e in rel).lower()
    paths = " ".join(f["path"] for f in files)
    tags = {t for e in rel for t in e["tags"]}
    # Д9 (изменение 4, GUI-4106): запись с тегом локализации упоминает локали или языки
    loc_tags = set(cls.get("locale_tags", ["i18n"]))
    loc_words = cls.get("locale_keywords", ["локал", "язык"])
    loc_hits = [e for e in rel if set(e["tags"]) & loc_tags and _kw(e["text"], loc_words)]
    if loc_hits:
        return {"value": "Д9", "why": f"записи с тегом {sorted(loc_tags)} упоминают локали или языки: {len(loc_hits)}"}
    if locale_added and ("локал" in texts or "язык" in texts or "i18n" in tags):
        return {"value": "Д9", "why": f"добавлены локали {locale_added}"}
    if "navigation" in tags or "/navigation/" in paths or "/routes" in paths or "меню" in texts:
        return {"value": "Д3", "why": "навигация / меню"}
    if "/permissions/" in paths or re.search(r"прав[оа]? доступ|роли", texts):
        return {"value": "Д6", "why": "права и роли"}
    # Д7 (изменение 3, GUI-4134): слова об удалении в записи-исправлении описывают способ исправления
    not_fix_texts = " ".join(e["text"] for e in rel if e["type"] != "fix").lower()
    if "remove" in types or re.search(r"\bудал[её]н|\bубран", not_fix_texts):
        return {"value": "Д7", "why": "удаление"}
    if re.search(r"валидац|обязательн|placeholder|по умолчанию|максимальн", texts):
        return {"value": "Д5", "why": "поля / валидация"}
    if i18n["changed"] and not i18n["added"]:
        return {"value": "Д4", "why": "текст изменён при том же ключе"}
    if "add" in types and "fix" not in types:
        return {"value": "Д2", "why": "добавлено"}
    if types and all(t == "fix" for t in types):
        return {"value": "Д8", "why": "исправление"}
    return {"value": "Д3", "why": "изменение поведения (по умолчанию)"}


def doc_candidates(files, rel_entries, doc_map, only: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Сначала совпадения по коду (пути файлов и теги). Слова из текста — только если по коду пусто.
    only - имена областей карты, которыми ограничен подбор (для Д9: только страница со списком языков)."""
    if only is not None:
        out_only: "OrderedDict[str, List[str]]" = OrderedDict()
        for m in doc_map["map"]:
            if m["name"] in only:
                for d in m["docs"]:
                    out_only.setdefault(d, []).append(f"область: {m['name']}")
        return [{"doc": d, "why": w} for d, w in out_only.items()]
    hay = [f["path"] for f in files] + ["/".join(e["tags"]) for e in rel_entries]
    hay_text = " ".join(e["text"] for e in rel_entries).lower()

    def collect(by_code: bool) -> "OrderedDict[str, List[str]]":
        out: "OrderedDict[str, List[str]]" = OrderedDict()
        for m in doc_map["map"]:
            hit = None
            if by_code:
                hit = next((f"код: {fr}" for fr in m.get("code", []) if any(fr in h for h in hay)), None)
            else:
                hit = next((f"слово: {w}" for w in m.get("words", []) if w.lower() in hay_text), None)
            if hit:
                for d in m["docs"]:
                    out.setdefault(d, []).append(hit)
        return out

    out = collect(True) or collect(False)
    return [{"doc": d, "why": sorted(set(w))} for d, w in out.items()]


def action_of(product, visibility, klass) -> Dict[str, str]:
    if product["value"] == "other":
        return {"value": "skip", "why": "другой продукт"}
    if product["value"] == "needs_review":
        return {"value": "review", "why": "продукт не определён однозначно"}
    v = visibility["value"]
    if v == "not_visible":
        return {"value": "skip", "why": "не видно пользователю"}
    if v == "style_only":
        return {"value": "screenshot_check", "why": "только визуальные правки"}
    if v == "unknown":
        return {"value": "review", "why": "видимость не определена"}
    if klass["value"] == "Д9":
        return {"value": "rn_and_check_langs", "why": "локализация: строка RN + проверить список языков в доке"}
    if klass["value"] == "Д8":
        return {"value": "rn_and_check_doc", "why": "исправление: строка RN + проверить, не описывала ли дока ошибочное поведение"}
    return {"value": "doc_and_rn", "why": "видимое изменение POINT 2"}


# ---------------------------------------------------------------- сборка

def build_units(changelog: Dict[str, Any], code: Dict[str, Any], reach: Set[str], i18n_rows_old, i18n_rows_new,
                i18n_diff_fn, locale_added: List[str], jira: Dict[str, Dict[str, Any]], rules, doc_map,
                i18n_usage: Optional[Dict[str, List[str]]] = None) -> List[Dict[str, Any]]:
    i18n_usage = i18n_usage or {}

    def only_other_product(keys: List[str]) -> bool:
        """Текст используется в коде, и все места использования - файлы другого продукта."""
        used = [p for k in keys for p in i18n_usage.get(k, [])]
        return bool(used) and all(is_other_product_path(p, rules) for p in used)

    changed = code["files"]
    by_path = {f["path"]: f for f in changed}
    # глобальные множества текстов по локалям: строка, перенесённая в другой файл, — не изменение
    g_old: Dict[str, Set[str]] = defaultdict(set)
    g_new: Dict[str, Set[str]] = defaultdict(set)
    for r in i18n_rows_old:
        g_old[r["locale"]].add(r["value"])
    for r in i18n_rows_new:
        g_new[r["locale"]].add(r["value"])

    groups: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for e in changelog["entries"]:
        e = dict(e)
        e["_markers"] = sorted(_markers(e, rules))
        e["_flags"] = entry_flags(e, rules)
        uid = e["task"] or f"NOTASK:{e['id']}"
        g = groups.setdefault(uid, {"id": uid, "task": e["task"], "entries": [], "_files": defaultdict(list),
                                    "_entry_files": defaultdict(set)})
        g["entries"].append(e)
        for p, why in files_for_entry(e, changed, rules).items():
            g["_files"][p].extend(f"{e['id']}: {w}" for w in why)
            g["_entry_files"][e["id"]].add(p)

    units = []
    for uid, g in groups.items():
        files = []
        for p, why in g["_files"].items():
            f = dict(by_path[p])
            f["reachable_from_app"] = p in reach
            f["linked_by"] = sorted(set(why))[:6]
            files.append(f)
        # значимые записи и файлы, привязанные только к ним (изменение 1): все доказательства считаются по ним
        rel_entries = significant_entries(g["entries"])
        # файлы другого продукта доказательством для POINT 2 не считаются (изменение 7)
        rel_paths = {p for e in rel_entries for p in g["_entry_files"].get(e["id"], set())
                     if not is_other_product_path(p, rules)}
        rel_files = [f for f in files if f["path"] in rel_paths]
        for f in files:
            f["relevant"] = f["path"] in rel_paths
        old_r = [r for r in i18n_rows_old if r["file"] in rel_paths]
        new_r = [r for r in i18n_rows_new if r["file"] in rel_paths]
        d = i18n_diff_fn(old_r, new_r)
        i18n = {
            "added": [{"locale": loc, **x} for loc, v in d["locales"].items() for x in v["added"]
                      if x["value"] not in g_old.get(loc, set())],
            "removed": [{"locale": loc, **x} for loc, v in d["locales"].items() for x in v["removed"]
                        if x["value"] not in g_new.get(loc, set())],
            "changed": [c for c in d["changed_same_key"]
                        if not (c["old"] in g_new.get(c["locale"], set()) and c["new"] in g_old.get(c["locale"], set()))],
        }
        foreign = [x for x in i18n["added"] if only_other_product(x["where"])]
        foreign += [c for c in i18n["changed"] if only_other_product([f"{c['file']}::{c['key']}"])]
        i18n["added"] = [x for x in i18n["added"] if not only_other_product(x["where"])]
        i18n["changed"] = [c for c in i18n["changed"] if not only_other_product([f"{c['file']}::{c['key']}"])]
        i18n["other_product"] = [{"locale": x["locale"], "value": x.get("value", x.get("new"))} for x in foreign]
        jira_issue = jira.get(g["task"]) if g["task"] else None
        product = decide_product(g, jira_issue, g["_entry_files"], reach, rules)
        visibility = decide_visibility(rel_entries, rel_files, i18n, rules)
        klass = decide_class(g, rel_files, i18n, locale_added, rules)
        action = action_of(product, visibility, klass)
        if action["value"] == "skip":
            cands = []
        elif action["value"] == "rn_and_check_langs":
            cands = doc_candidates(rel_files, rel_entries, doc_map, only=rules.get("class", {}).get("locale_doc_areas", ["locales"]))
        else:
            cands = doc_candidates(rel_files, rel_entries, doc_map)
        units.append({
            "id": uid,
            "task": g["task"],
            "jira": ({k: jira_issue.get(k) for k in ("summary", "product_tags", "type", "status", "labels")}
                     | {"description": (jira_issue.get("description") or "")[:2000]}) if jira_issue else None,
            "versions": sorted({e["version"] for e in g["entries"]}),
            "entries": [{k: e[k] for k in ("id", "version", "section", "type", "tags", "text", "_markers", "_flags")} for e in g["entries"]],
            "files": sorted(files, key=lambda f: f["path"]),
            "i18n": i18n,
            "decision": {"product": product, "visibility": visibility, "class_draft": klass, "action": action},
            "doc_candidates": cands,
        })
    return units


def unattributed_files(units, code) -> List[Dict[str, Any]]:
    used = {f["path"] for u in units for f in u["files"]}
    return [f for f in code["files"] if f["path"] not in used]
