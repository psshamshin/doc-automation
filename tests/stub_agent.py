"""Заглушка агента для тестов. Запускается в папке агента, читает задание из stdin, пишет ответ.

Правки возвращает как данные (find/replace), файлы документации не меняет.
Режим - первый параметр: ok (по умолчанию); no_answer, bad_json, fail - сбои; no_evidence, fake_quote, foreign_step,
no_steps, fake_term, bad_find, wrong_page, comment, big_edit, shot - ответы, которые код должен отклонить;
unknown - агент не нашел фактов; direct - правит файл документации напрямую; bad_then_ok - первая попытка плохая,
повторная хорошая; rn_no_line, rn_missing, rn_code - варианты release notes.
"""

import json
import re
import sys
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
prompt = sys.stdin.read()
item = re.search(r"^# Задание (\S+)", prompt, flags=re.M).group(1)
kind = re.search(r"Вид задания: `(\w+)`", prompt).group(1)
answer = Path(re.search(r"^Файл ответа: `([^`]+)`", prompt, flags=re.M).group(1))
if mode == "bad_then_ok":
    mode = "ok" if "Повторная попытка" in prompt else "fake_quote"

if mode == "fail":
    sys.exit(3)
if mode == "no_answer":
    sys.exit(0)
answer.parent.mkdir(parents=True, exist_ok=True)
if mode == "bad_json":
    answer.write_text("не json", encoding="utf-8")
    sys.exit(0)

m = re.search(r"^- \[[^\]]+\] \[[^\]]*\] \[.*?\] (.+)$", prompt, flags=re.M)
quote = m.group(1).strip()[:60] if m else ""
ev = [{"source": "changelog", "quote": quote}] if quote else []
ev = {"no_evidence": [], "fake_quote": [{"source": "changelog", "quote": "такой фразы в записях задачи нет"}]}.get(mode, ev)
data = {"status": "done", "summary": f"заглушка выполнила {item}", "evidence": ev, "questions": ["замечание"],
        "steps": [] if mode == "no_steps" else [{"source": "docs/TODO.md" if mode == "foreign_step" else "changelog",
                                                  "found": "прочитал записи задачи"}]}
with open("progress.md", "a", encoding="utf-8") as f:
    f.write(f"НАЧАЛ {item}\nГОТОВО {item}\n")
if mode == "unknown":
    data["status"] = "unknown"
if kind == "decide":
    data["decision"] = "rn" if "GUI-103" in item else "skip"
if kind == "write_doc" and mode != "unknown":
    page = re.findall(r"^- `(docs/MD_[^`]+)`$", prompt, flags=re.M)[0]
    first = Path(page).read_text(encoding="utf-8").splitlines()[0]
    new = first + f"\n\nПравка заглушки {item}."
    new += {"fake_term": "\nНажмите кнопку «Несуществующая кнопка».", "comment": "\n<!-- вопрос -->",
            "shot": '\n!!! warning "SCREENSHOT-TODO GUI-1"', "big_edit": "".join(f"\nСтрока {n}." for n in range(40))}.get(mode, "")
    find = "такой строки на странице нет" if mode == "bad_find" else first
    if mode == "wrong_page":
        page = "docs/MD_rus/docs/index.md"
        find = Path(page).read_text(encoding="utf-8").splitlines()[0]
    if mode == "direct":
        other = Path("docs/MD_rus/docs/index.md")
        other.write_text(other.read_text(encoding="utf-8") + "\nПрямая правка.\n", encoding="utf-8")
    data["edits"] = [{"file": page, "find": find, "replace": new, "what": "добавлена строка", "evidence": ev}]
if kind == "release_notes":
    tasks = re.findall(r"^### (\S+) \((.*)\)$", prompt, flags=re.M)
    lines = []
    for uid, state in tasks:
        if "решение принимаешь ты" in state and uid != "GUI-103":
            continue                          # заглушка решает skip по всем спорным задачам, кроме GUI-103
        ru = "Переведено на resetFilters из `useFilters`." if mode == "rn_code" else f"Изменение по задаче {uid}."
        lines.append({"task": uid, "section": "Общее", "ru": ru, "en": f"Change for task {uid}."})
    data = {"lines": lines}
    if mode == "rn_no_line":
        skipped = lines.pop()
        data["no_line"] = [{"task": skipped["task"], "reason": "пользователю не видно"}]
    if mode == "rn_missing":
        lines.pop()
answer.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
