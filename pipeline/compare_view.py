"""Страница сравнения в три колонки: было (прошлый релиз), пайплайн, техпис.

Один файл compare.html без внешних зависимостей. Опорная колонка - документация прошлого релиза: к каждой
ее строке привязано, что с ней сделал пайплайн и что сделал техпис. Изменения выделены цветом, внутри
измененной строки выделены изменившиеся слова.
"""

from __future__ import annotations

import difflib
import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import docs as DOCS
from .common import read_text

TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)


def _words(old: str, new: str) -> Tuple[str, str]:
    """Две версии строки с выделенными изменившимися словами."""
    a, b = TOKEN_RE.findall(old), TOKEN_RE.findall(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    oa: List[str] = []
    ob: List[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        x, y = html.escape("".join(a[i1:i2])), html.escape("".join(b[j1:j2]))
        if tag == "equal":
            oa.append(x)
            ob.append(y)
        else:
            if x:
                oa.append(f"<mark>{x}</mark>")
            if y:
                ob.append(f"<mark>{y}</mark>")
    return "".join(oa), "".join(ob)


def _side(base: List[str], other: Optional[List[str]]) -> Dict[str, Any]:
    """Что другая версия сделала с каждой строкой базы и что вставила перед ней."""
    n = len(base)
    state: List[Any] = [("eq", None, None)] * n      # (вид, строка другой версии, строка базы с выделением)
    ins: List[List[str]] = [[] for _ in range(n + 1)]
    if other is None:                                 # файла в этой версии нет
        return {"state": [("del", None, None)] * n, "ins": ins, "missing": True}
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                state[i1 + k] = ("eq", html.escape(other[j1 + k]), None)
        elif tag == "delete":
            for i in range(i1, i2):
                state[i] = ("del", None, None)
        elif tag == "insert":
            ins[i1] += [html.escape(x) for x in other[j1:j2]]
        else:
            m = min(i2 - i1, j2 - j1)
            for k in range(m):
                bo, ot = _words(base[i1 + k], other[j1 + k])
                state[i1 + k] = ("chg", ot, bo)
            for i in range(i1 + m, i2):
                state[i] = ("del", None, None)
            ins[i2] += [html.escape(x) for x in other[j1 + m:j2]]
    return {"state": state, "ins": ins, "missing": False}


def file_rows(base: Optional[str], gen: Optional[str], ref: Optional[str]) -> Dict[str, Any]:
    """Строки таблицы: [база, пайплайн, техпис, вид пайплайна, вид техписа, номер строки базы]."""
    b = (base or "").splitlines()
    g = _side(b, gen.splitlines() if gen is not None else None)
    r = _side(b, ref.splitlines() if ref is not None else None)
    if base is None:            # нового файла в базе нет: все строки - вставки
        g["missing"] = gen is None
        r["missing"] = ref is None
    rows: List[List[Any]] = []
    stats = {"same": 0, "diff": 0, "gen": 0, "ref": 0}

    def count(gk: str, rk: str, gt, rt) -> None:
        if gk == "eq" and rk == "eq":
            return
        if gk != "eq" and rk != "eq":
            stats["same" if (gk == rk and gt == rt) else "diff"] += 1
        elif gk != "eq":
            stats["gen"] += 1
        else:
            stats["ref"] += 1

    for i in range(len(b) + 1):
        gi, ri = g["ins"][i], r["ins"][i]
        for k in range(max(len(gi), len(ri))):
            gt = gi[k] if k < len(gi) else None
            rt = ri[k] if k < len(ri) else None
            gk, rk = ("add" if gt is not None else "eq"), ("add" if rt is not None else "eq")
            count(gk, rk, gt, rt)
            rows.append(["", gt or "", rt or "", gk if gt is not None else "pad", rk if rt is not None else "pad", 0])
        if i < len(b):
            gk, gt, gb = g["state"][i]
            rk, rt, rb = r["state"][i]
            count(gk, rk, gt, rt)
            rows.append([gb or rb or html.escape(b[i]), gt or "", rt or "", gk, rk, i + 1])
    return {"rows": rows, "stats": stats, "gen_missing": g["missing"] and gen is None, "ref_missing": r["missing"] and ref is None,
            "base_missing": base is None}


def build_data(base_repo: Path, result_dir: Path, ref_repo: Path, langs: List[str]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for lang in langs:
        tb = DOCS._text_files(DOCS.docs_root(base_repo, lang))
        tg = DOCS._text_files(DOCS.workspace_root(result_dir, lang))
        tr = DOCS._text_files(DOCS.docs_root(ref_repo, lang))
        files = []
        for rel in sorted(set(tb) | set(tg) | set(tr)):
            b = read_text(tb[rel]) if rel in tb else None
            g = read_text(tg[rel]) if rel in tg else None
            r = read_text(tr[rel]) if rel in tr else None
            if g is None and rel in tb and rel not in tg:
                g = None
            gen_changed, ref_changed = g != b, r != b
            if not gen_changed and not ref_changed:
                continue
            fr = file_rows(b, g, r)
            kind = "both" if gen_changed and ref_changed else "gen" if gen_changed else "ref"
            files.append({"path": rel, "kind": kind, **fr})
        data[lang] = files
    return data


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1b2230;--muted:#5d6779;--line:#d9dee6;--acc:#2456c8;
--add:#dff3e4;--addm:#a8e0b5;--del:#fbe3e3;--delm:#f3b1b1;--chg:#fff4d1;--chgm:#ffd76a;--pad:#eef0f4;--same:#1f8f4d;--diff:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#11141a;--card:#191d25;--fg:#e3e7ef;--muted:#98a2b5;--line:#2b323f;--acc:#86a8ff;
--add:#143021;--addm:#1f6b3a;--del:#3a1b1d;--delm:#7d2d31;--chg:#3a3113;--chgm:#7a5f0c;--pad:#161a21;--same:#4ade80;--diff:#fb923c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px}
h1{font-size:16px;margin:0 0 8px}
.bar{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center}
.bar label{color:var(--muted);font-size:13px}
select,button{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 8px}
select#file{min-width:min(520px,100%);max-width:100%}
button{cursor:pointer} button.on{border-color:var(--acc);color:var(--acc)} button:disabled{opacity:.4;cursor:default}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:12px;color:var(--muted)}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:4px;border:1px solid var(--line)}
.stats{font-size:13px;margin-top:6px}
.stats b.s{color:var(--same)} .stats b.d{color:var(--diff)}
main{padding:12px 16px 40px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;table-layout:fixed;min-width:900px}
th{position:sticky;top:0;background:var(--card);text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px}
th small{display:block;font-weight:400;color:var(--muted)}
col.n{width:44px} col.m{width:26px}
td{vertical-align:top;padding:1px 10px;border-left:1px solid var(--line);font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
td.n{color:var(--muted);text-align:right;border-left:0;user-select:none;padding:1px 6px}
td.m{text-align:center;padding:1px 0;font-weight:700;user-select:none}
td.m.same{color:var(--same)} td.m.diff{color:var(--diff)}
td.add{background:var(--add)} td.del{background:var(--del)} td.chg{background:var(--chg)} td.pad{background:var(--pad)}
td.add mark,td.chg mark{background:var(--chgm);color:inherit;border-radius:2px}
td.add{box-shadow:inset 3px 0 var(--addm)} td.del{box-shadow:inset 3px 0 var(--delm)} td.chg{box-shadow:inset 3px 0 var(--chgm)}
td.base mark{background:var(--delm);color:inherit;border-radius:2px}
tr.gap td{background:var(--bg);color:var(--muted);text-align:center;font:12px system-ui;padding:3px;border-left:0}
.note{padding:10px 12px;color:var(--muted)}
</style></head><body>
<header>
<h1>__TITLE__</h1>
<div class="bar">
  <label>Язык <select id="lang"></select></label>
  <label>Показать <select id="kind">
    <option value="all">все измененные файлы</option><option value="both">изменили оба</option>
    <option value="gen">только пайплайн</option><option value="ref">только техпис</option></select></label>
  <label>Файл <select id="file"></select></label>
  <button id="prev" title="Предыдущий файл">&larr;</button><button id="next" title="Следующий файл">&rarr;</button>
  <button id="whole">Весь файл</button>
</div>
<div class="stats" id="stats"></div>
<div class="legend">
  <span><i class="sw" style="background:var(--add)"></i>добавлено</span>
  <span><i class="sw" style="background:var(--del)"></i>удалено</span>
  <span><i class="sw" style="background:var(--chg)"></i>изменено, слова выделены</span>
  <span><b style="color:var(--same)">=</b> пайплайн и техпис сделали одно и то же</span>
  <span><b style="color:var(--diff)">&ne;</b> оба изменили, но по-разному</span>
  <span><b>&lsaquo;</b> только пайплайн</span><span><b>&rsaquo;</b> только техпис</span>
</div>
</header>
<main><div class="wrap"><table>
<colgroup><col class="n"><col><col><col class="m"><col></colgroup>
<thead><tr><th></th><th>Было<small>документация прошлого релиза</small></th><th>Пайплайн<small>код и агент</small></th><th></th><th>Техпис<small>документация нового релиза</small></th></tr></thead>
<tbody id="rows"></tbody></table><div class="note" id="note"></div></div></main>
<script>
const DATA = __DATA__;
const CONTEXT = 3;
const $ = id => document.getElementById(id);
let whole = false;
const KIND = {both: "оба", gen: "пайплайн", ref: "техпис"};
function files(){ const k = $("kind").value; return (DATA[$("lang").value] || []).filter(f => k === "all" || f.kind === k); }
function fillFiles(){
  const sel = $("file"), list = files(); sel.innerHTML = "";
  list.forEach((f, i) => { const o = document.createElement("option"); o.value = i;
    const s = f.stats; o.textContent = `${f.path}  [${KIND[f.kind]}: = ${s.same}, ≠ ${s.diff}, ‹ ${s.gen}, › ${s.ref}]`; sel.appendChild(o); });
  render();
}
function mark(g, r, row){
  const gc = g !== "eq" && g !== "pad", rc = r !== "eq" && r !== "pad";
  if (gc && rc) return (g === r && row[1] === row[2]) ? ["=", "same"] : ["≠", "diff"];
  if (gc) return ["‹", ""]; if (rc) return ["›", ""]; return ["", ""];
}
function render(){
  const list = files(), f = list[+$("file").value], body = $("rows"); body.innerHTML = "";
  $("prev").disabled = !f || +$("file").value === 0; $("next").disabled = !f || +$("file").value >= list.length - 1;
  if (!f){ $("stats").textContent = ""; $("note").textContent = "Нет файлов для выбранных условий."; return; }
  const s = f.stats;
  $("stats").innerHTML = `Файлов в списке: ${list.length}. В этом файле: совпало <b class="s">${s.same}</b>, по-разному <b class="d">${s.diff}</b>, только пайплайн <b>${s.gen}</b>, только техпис <b>${s.ref}</b>.`;
  const notes = [];
  if (f.base_missing) notes.push("Файла не было в прошлом релизе: это новый файл.");
  if (f.gen_missing) notes.push("У пайплайна этого файла нет.");
  if (f.ref_missing) notes.push("У техписа этого файла нет.");
  $("note").textContent = notes.join(" ");
  const rows = f.rows, changed = rows.map(r => !((r[3] === "eq") && (r[4] === "eq")));
  const show = rows.map((_, i) => whole);
  if (!whole) changed.forEach((c, i) => { if (c) for (let k = Math.max(0, i - CONTEXT); k <= Math.min(rows.length - 1, i + CONTEXT); k++) show[k] = true; });
  const frag = document.createDocumentFragment(); let skipped = 0;
  const gap = () => { if (!skipped) return; const tr = document.createElement("tr"); tr.className = "gap";
    tr.innerHTML = `<td colspan="5">… без изменений: ${skipped} стр.</td>`; frag.appendChild(tr); skipped = 0; };
  rows.forEach((r, i) => {
    if (!show[i]){ skipped++; return; } gap();
    const [m, mc] = mark(r[3], r[4], r), tr = document.createElement("tr");
    const baseCls = (r[5] === 0) ? "pad" : ((r[3] === "del" || r[4] === "del") ? "del" : ((r[3] === "chg" || r[4] === "chg") ? "base chg" : ""));
    tr.innerHTML = `<td class="n">${r[5] || ""}</td><td class="${baseCls}">${r[0]}</td><td class="${r[3] === "eq" ? "" : r[3]}">${r[1]}</td><td class="m ${mc}">${m}</td><td class="${r[4] === "eq" ? "" : r[4]}">${r[2]}</td>`;
    frag.appendChild(tr);
  });
  gap(); body.appendChild(frag); window.scrollTo(0, 0);
}
Object.keys(DATA).forEach(l => { const o = document.createElement("option"); o.value = l; o.textContent = l + " (" + DATA[l].length + ")"; $("lang").appendChild(o); });
$("lang").onchange = fillFiles; $("kind").onchange = fillFiles; $("file").onchange = render;
$("prev").onclick = () => { $("file").value = +$("file").value - 1; render(); };
$("next").onclick = () => { $("file").value = +$("file").value + 1; render(); };
$("whole").onclick = () => { whole = !whole; $("whole").classList.toggle("on", whole); render(); };
fillFiles();
</script></body></html>
"""


def render_page(data: Dict[str, Any], title: str) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return PAGE.replace("__TITLE__", html.escape(title)).replace("__DATA__", payload)
