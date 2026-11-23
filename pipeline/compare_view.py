"""Страница сравнения в три окна: было (прошлый релиз), пайплайн, техпис.

Один файл compare.html без внешних зависимостей. Опорная колонка - документация прошлого релиза: к каждой
ее строке привязано, что с ней сделал пайплайн и что сделал техпис. Изменения выделены цветом, внутри
измененной строки выделены изменившиеся слова. Строки не переносятся (таблицы не ломаются): у каждого окна
своя горизонтальная прокрутка, по вертикали окна прокручиваются вместе, масштаб общий.
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

PAIR_MIN = 0.45                  # насколько похожи строки, чтобы считать одну измененной версией другой
TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)


def _words(old: str, new: str) -> Tuple[str, str]:
    """Две версии строки с выделенными изменившимися словами."""
    a, b = TOKEN_RE.findall(old), TOKEN_RE.findall(new)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    oa: List[str] = []
    ob: List[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            oa.append(html.escape("".join(a[i1:i2])))
            ob.append(html.escape("".join(b[j1:j2])))
        else:
            oa.append(_marked(a[i1:i2]))
            ob.append(_marked(b[j1:j2]))
    return "".join(oa), "".join(ob)


def _marked(tokens: List[str]) -> str:
    """Выделяются слова, а не пробелы по краям: выравнивание таблиц не дает сплошных цветных полос."""
    idx = [k for k, t in enumerate(tokens) if t.strip()]
    if not idx:
        return html.escape("".join(tokens))
    lo, hi = idx[0], idx[-1] + 1
    return (html.escape("".join(tokens[:lo])) + "<mark>" + html.escape("".join(tokens[lo:hi])) + "</mark>"
            + html.escape("".join(tokens[hi:])))


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
            # строки заменяемого блока сопоставляются по похожести, а не по порядку: слова выделяются в нужной паре
            p = j1
            for i in range(i1, i2):
                best, bj = 0.0, None
                for j in range(p, j2):
                    sm2 = difflib.SequenceMatcher(None, base[i], other[j], autojunk=False)
                    if sm2.real_quick_ratio() > best and sm2.quick_ratio() > best:
                        r = sm2.ratio()
                        if r > best:
                            best, bj = r, j
                if bj is not None and best >= PAIR_MIN:
                    ins[i] += [html.escape(x) for x in other[p:bj]]
                    bo, ot = _words(base[i], other[bj])
                    state[i] = ("chg", ot, bo)
                    p = bj + 1
                else:
                    state[i] = ("del", None, None)
            ins[i2] += [html.escape(x) for x in other[p:j2]]
    return {"state": state, "ins": ins, "missing": False}


def _new_file_rows(gen: List[str], ref: List[str]) -> Dict[str, Any]:
    """Новый файл есть и у пайплайна, и у техписа: строки выравниваются друг по другу, похожие - в одну строку."""
    rows: List[List[Any]] = []
    stats = {"same": 0, "diff": 0, "gen": 0, "ref": 0}

    def put(g: Optional[str], r: Optional[str]) -> None:
        if g is not None and r is not None:
            if g == r:
                rows.append(["", html.escape(g), html.escape(r), "add", "add", 0])
                stats["same"] += 1
            else:
                gg, rr = _words(g, r)
                rows.append(["", gg, rr, "chg", "chg", 0])
                stats["diff"] += 1
        elif g is not None:
            rows.append(["", html.escape(g), "", "add", "pad", 0])
            stats["gen"] += 1
        else:
            rows.append(["", "", html.escape(r), "pad", "add", 0])
            stats["ref"] += 1

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, gen, ref, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                put(gen[i1 + k], ref[j1 + k])
            continue
        p = j1
        for i in range(i1, i2):
            best, bj = 0.0, None
            for j in range(p, j2):
                r = difflib.SequenceMatcher(None, gen[i], ref[j], autojunk=False).ratio()
                if r > best:
                    best, bj = r, j
            if bj is not None and best >= PAIR_MIN:
                for j in range(p, bj):
                    put(None, ref[j])
                put(gen[i], ref[bj])
                p = bj + 1
            else:
                put(gen[i], None)
        for j in range(p, j2):
            put(None, ref[j])
    return {"rows": rows, "stats": stats, "gen_missing": False, "ref_missing": False, "base_missing": True}


def file_rows(base: Optional[str], gen: Optional[str], ref: Optional[str]) -> Dict[str, Any]:
    """Строки таблицы: [база, пайплайн, техпис, вид пайплайна, вид техписа, номер строки базы]."""
    if base is None and gen is not None and ref is not None:
        return _new_file_rows(gen.splitlines(), ref.splitlines())
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
:root{--bg:#f4f5f7;--card:#fff;--fg:#1b2230;--muted:#5d6779;--line:#d9dee6;--acc:#2456c8;
--add:#e3f5e8;--addm:#2e9e57;--del:#fde8e8;--delm:#d64545;--chg:#fff6d8;--chgm:#f5c542;--word:#ffd54a;--wordd:#f7a9a9;
--pad:repeating-linear-gradient(135deg,#eef0f4 0 6px,#f6f7f9 6px 12px);--same:#1f8f4d;--diff:#c2410c;--fs:13px}
@media (prefers-color-scheme:dark){:root{--bg:#101318;--card:#181c23;--fg:#e3e7ef;--muted:#98a2b5;--line:#2b323f;--acc:#86a8ff;
--add:#12301f;--addm:#3fbf6e;--del:#3a1a1c;--delm:#f06b6b;--chg:#352d12;--chgm:#d9a91d;--word:#8a6a00;--wordd:#7d2d31;
--pad:repeating-linear-gradient(135deg,#161a21 0 6px,#1b2028 6px 12px);--same:#4ade80;--diff:#fb923c}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;flex-direction:column}
header{flex:none;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px}
h1{font-size:16px;margin:0 0 8px}
.bar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center}
.bar label{color:var(--muted);font-size:13px;display:flex;gap:6px;align-items:center}
select,button,input{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
select#file{min-width:min(460px,100%);max-width:100%}
button{cursor:pointer} button.on{border-color:var(--acc);color:var(--acc)} button:disabled{opacity:.4;cursor:default}
.grp{display:flex;gap:4px;align-items:center}
input[type=range]{padding:0;width:120px;accent-color:var(--acc)}
#zoomv{min-width:44px;text-align:right;color:var(--muted);font-size:13px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:12px;color:var(--muted)}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:4px;border:1px solid var(--line)}
.stats{font-size:13px;margin-top:6px}
.stats b.s{color:var(--same)} .stats b.d{color:var(--diff)}
main{flex:1;min-height:0;padding:10px 16px 12px}
.panes{height:100%;display:grid;grid-template-rows:minmax(0,1fr);grid-template-columns:minmax(0,1fr) minmax(0,1fr) 28px minmax(0,1fr);border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--card)}
.pane{display:flex;flex-direction:column;min-width:0;min-height:0;border-left:1px solid var(--line)}
.pane:first-child{border-left:0}
.ph{flex:none;padding:7px 10px;border-bottom:1px solid var(--line);font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ph small{font-weight:400;color:var(--muted);margin-left:6px}
.sc{flex:1;min-height:0;overflow:auto;font:var(--fs)/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.pane.marks .sc{overflow:hidden}
.in{display:inline-block;min-width:100%}
.r{height:calc(var(--fs)*1.5);line-height:calc(var(--fs)*1.5);white-space:pre;padding:0 10px 0 8px;border-left:3px solid transparent}
.r.add{background:var(--add);border-left-color:var(--addm)}
.r.del{background:var(--del);border-left-color:var(--delm)}
.r.chg{background:var(--chg);border-left-color:var(--chgm)}
.r.pad{background:var(--pad)}
.r mark{background:var(--word);color:inherit;border-radius:2px;padding:0 1px}
.base .r mark{background:var(--wordd)}
.r.cur{outline:2px solid var(--acc);outline-offset:-2px}
.ln{position:sticky;left:0;display:inline-block;width:4.2em;margin:0 8px 0 -8px;padding-right:8px;text-align:right;color:var(--muted);background:var(--card);user-select:none}
.r.add .ln{background:var(--add)} .r.del .ln{background:var(--del)} .r.chg .ln{background:var(--chg)} .r.pad .ln{background:var(--pad)}
.base .r{padding-left:8px}
.marks .r{padding:0;text-align:center;font-weight:700;border-left:0}
.marks .r.same{color:var(--same)} .marks .r.diff{color:var(--diff)}
.r.gap{background:var(--bg);color:var(--muted);font:12px/calc(var(--fs)*1.5) system-ui;border-left:0;cursor:pointer}
.note{color:var(--muted);font-size:13px;margin-top:4px}
kbd{font:11px ui-monospace,monospace;border:1px solid var(--line);border-radius:4px;padding:0 4px;background:var(--bg)}
</style></head><body>
<header>
<h1>__TITLE__</h1>
<div class="bar">
  <label>Язык <select id="lang"></select></label>
  <label>Показать <select id="kind">
    <option value="all">все измененные файлы</option><option value="both">изменили оба</option>
    <option value="gen">только пайплайн</option><option value="ref">только техпис</option></select></label>
  <label>Файл <select id="file"></select></label>
  <span class="grp"><button id="prev" title="Предыдущий файл">&larr;</button><button id="next" title="Следующий файл">&rarr;</button></span>
  <span class="grp"><button id="chprev" title="Предыдущее изменение (k)">&uarr; изменение</button><button id="chnext" title="Следующее изменение (j)">&darr; изменение</button></span>
  <button id="whole" title="Показывать неизмененные строки">Весь файл</button>
  <label title="Горизонтальная прокрутка всех окон вместе"><input type="checkbox" id="syncx" checked> вместе по горизонтали</label>
  <span class="grp" title="Масштаб всех трех окон (Ctrl или Cmd + колесо мыши над окнами)">
    <button id="zout">A&minus;</button><input type="range" id="zoom" min="60" max="200" step="10" value="100"><button id="zin">A+</button>
    <span id="zoomv">100%</span><button id="zreset" title="Сбросить масштаб">100%</button></span>
</div>
<div class="stats" id="stats"></div>
<div class="legend">
  <span><i class="sw" style="background:var(--add)"></i>добавлено</span>
  <span><i class="sw" style="background:var(--del)"></i>удалено</span>
  <span><i class="sw" style="background:var(--chg)"></i>изменено</span>
  <span><i class="sw" style="background:var(--word)"></i>измененные слова</span>
  <span><b style="color:var(--same)">=</b> пайплайн и техпис сделали одно и то же</span>
  <span><b style="color:var(--diff)">&ne;</b> оба изменили, но по-разному</span>
  <span><b>&lsaquo;</b> только пайплайн</span><span><b>&rsaquo;</b> только техпис</span>
  <span><kbd>j</kbd>/<kbd>k</kbd> следующее и предыдущее изменение</span>
</div>
<div class="note" id="note"></div>
</header>
<main><div class="panes" id="panes">
  <div class="pane base"><div class="ph">Было<small>документация прошлого релиза</small></div><div class="sc" id="sc0"><div class="in" id="p0"></div></div></div>
  <div class="pane"><div class="ph">Пайплайн<small>код и агент</small></div><div class="sc" id="sc1"><div class="in" id="p1"></div></div></div>
  <div class="pane marks"><div class="ph">&nbsp;</div><div class="sc" id="sc2"><div class="in" id="p2"></div></div></div>
  <div class="pane"><div class="ph">Техпис<small>документация нового релиза</small></div><div class="sc" id="sc3"><div class="in" id="p3"></div></div></div>
</div></main>
<script>
const DATA = __DATA__;
const CONTEXT = 3;
const $ = id => document.getElementById(id);
const SC = [0, 1, 2, 3].map(i => $("sc" + i)), PN = [0, 1, 2, 3].map(i => $("p" + i));
let whole = false, changes = [], cur = -1;
const KIND = {both: "оба", gen: "пайплайн", ref: "техпис"};
const store = {get(k){ try { return localStorage.getItem(k); } catch (e) { return null; } },
               set(k, v){ try { localStorage.setItem(k, v); } catch (e) {} }};

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
function cls(k){ return k === "eq" ? "" : k; }
function render(){
  const list = files(), f = list[+$("file").value];
  PN.forEach(p => p.innerHTML = "");
  $("prev").disabled = !f || +$("file").value === 0; $("next").disabled = !f || +$("file").value >= list.length - 1;
  changes = []; cur = -1;
  if (!f){ $("stats").textContent = ""; $("note").textContent = "Нет файлов для выбранных условий."; return; }
  const s = f.stats;
  $("stats").innerHTML = `Файлов в списке: ${list.length}. В этом файле: совпало <b class="s">${s.same}</b>, по-разному <b class="d">${s.diff}</b>, только пайплайн <b>${s.gen}</b>, только техпис <b>${s.ref}</b>.`;
  const notes = [];
  if (f.base_missing) notes.push("Файла не было в прошлом релизе: это новый файл.");
  if (f.gen_missing) notes.push("У пайплайна этого файла нет.");
  if (f.ref_missing) notes.push("У техписа этого файла нет.");
  $("note").textContent = notes.join(" ");
  const rows = f.rows, changed = rows.map(r => !(r[3] === "eq" && r[4] === "eq"));
  const show = rows.map(() => whole);
  if (!whole) changed.forEach((c, i) => { if (c) for (let k = Math.max(0, i - CONTEXT); k <= Math.min(rows.length - 1, i + CONTEXT); k++) show[k] = true; });
  const out = [[], [], [], []]; let skipped = 0, n = 0, prevChanged = false;
  const gap = () => { if (!skipped) return; const t = `… без изменений: ${skipped} стр.`;
    out.forEach((o, i) => o.push(`<div class="r gap" data-gap="1">${i === 2 ? "" : t}</div>`)); n++; skipped = 0; };
  rows.forEach((r, i) => {
    if (!show[i]){ skipped++; prevChanged = false; return; } gap();
    if (changed[i] && !prevChanged) changes.push(n);
    prevChanged = changed[i];
    const [m, mc] = mark(r[3], r[4], r);
    const baseCls = r[5] === 0 ? "pad" : ((r[3] === "del" || r[4] === "del") ? "del" : ((r[3] === "chg" || r[4] === "chg") ? "chg" : ""));
    out[0].push(`<div class="r ${baseCls}"><span class="ln">${r[5] || ""}</span>${r[0]}</div>`);
    out[1].push(`<div class="r ${cls(r[3])}">${r[1]}</div>`);
    out[2].push(`<div class="r ${mc}">${m}</div>`);
    out[3].push(`<div class="r ${cls(r[4])}">${r[2]}</div>`);
    n++;
  });
  gap();
  PN.forEach((p, i) => p.innerHTML = out[i].join(""));
  SC.forEach(sc => { sc.scrollTop = 0; sc.scrollLeft = 0; });
  $("chprev").disabled = $("chnext").disabled = !changes.length;
}
// прокрутка: по вертикали окна всегда вместе, по горизонтали - если включено
let lock = false;
SC.forEach((sc, i) => sc.addEventListener("scroll", () => {
  if (lock) return; lock = true;
  SC.forEach((o, j) => { if (j === i) return; o.scrollTop = sc.scrollTop; if ($("syncx").checked && j !== 2 && i !== 2) o.scrollLeft = sc.scrollLeft; });
  requestAnimationFrame(() => { lock = false; });
}));
// переход по изменениям
function rowH(){ const r = PN[1].firstElementChild; return r ? r.getBoundingClientRect().height : 20; }
function goChange(d){
  if (!changes.length) return;
  cur = cur < 0 ? (d > 0 ? 0 : changes.length - 1) : Math.min(changes.length - 1, Math.max(0, cur + d));
  const idx = changes[cur];
  PN.forEach(p => { p.querySelectorAll(".r.cur").forEach(x => x.classList.remove("cur")); const r = p.children[idx]; if (r) r.classList.add("cur"); });
  SC[1].scrollTop = Math.max(0, idx * rowH() - SC[1].clientHeight / 3);
}
// масштаб: один на все три окна
function setZoom(z){
  z = Math.max(60, Math.min(200, Math.round(z / 10) * 10));
  $("zoom").value = z; $("zoomv").textContent = z + "%";
  $("panes").style.setProperty("--fs", (13 * z / 100).toFixed(1) + "px");
  store.set("cmpZoom", String(z));
}
$("zoom").oninput = e => setZoom(+e.target.value);
$("zin").onclick = () => setZoom(+$("zoom").value + 10);
$("zout").onclick = () => setZoom(+$("zoom").value - 10);
$("zreset").onclick = () => setZoom(100);
$("panes").addEventListener("wheel", e => { if (!(e.ctrlKey || e.metaKey)) return; e.preventDefault(); setZoom(+$("zoom").value + (e.deltaY < 0 ? 10 : -10)); }, {passive: false});
document.addEventListener("keydown", e => {
  if (["INPUT", "SELECT"].includes(e.target.tagName) && e.target.type !== "checkbox" && e.target.type !== "range") return;
  if (e.key === "j") goChange(1); else if (e.key === "k") goChange(-1);
  else if ((e.ctrlKey || e.metaKey) && (e.key === "=" || e.key === "+")){ e.preventDefault(); setZoom(+$("zoom").value + 10); }
  else if ((e.ctrlKey || e.metaKey) && e.key === "-"){ e.preventDefault(); setZoom(+$("zoom").value - 10); }
});
PN.forEach(p => p.addEventListener("click", e => { if (e.target.dataset && e.target.dataset.gap){ whole = true; $("whole").classList.add("on"); render(); } }));
Object.keys(DATA).forEach(l => { const o = document.createElement("option"); o.value = l; o.textContent = l + " (" + DATA[l].length + ")"; $("lang").appendChild(o); });
$("lang").onchange = fillFiles; $("kind").onchange = fillFiles; $("file").onchange = render;
$("prev").onclick = () => { $("file").value = +$("file").value - 1; render(); };
$("next").onclick = () => { $("file").value = +$("file").value + 1; render(); };
$("chprev").onclick = () => goChange(-1); $("chnext").onclick = () => goChange(1);
$("whole").onclick = () => { whole = !whole; $("whole").classList.toggle("on", whole); render(); };
setZoom(+(store.get("cmpZoom") || 100));
fillFiles();
</script></body></html>
"""


def render_page(data: Dict[str, Any], title: str) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return PAGE.replace("__TITLE__", html.escape(title)).replace("__DATA__", payload)
