"""Заглушка агента для тестов. Запускается в рабочей папке агента, читает задание из stdin, пишет ответ.

Правки заглушка возвращает как данные (find/replace), файлы документации не меняет.

Режим задается первым параметром:
ok (по умолчанию) - правка первой страницы задания, подтвержденная цитатой из записей задачи;
no_answer, bad_json, fail - сбои;
no_evidence - ответ без подтверждений; fake_quote - цитата, которой нет в источнике;
foreign_source - ссылка на файл вне разрешенных; no_steps - нет журнала шагов;
foreign_step - в шагах файл вне источников задачи; fake_term - выдуманное название элемента интерфейса;
big_edit - правка длиннее допустимого; bad_find - фрагмента нет на странице; wrong_page - правка чужой страницы;
comment - в документацию пишется комментарий с вопросом; shot - агент ставит метку скриншота; direct - агент правит файл напрямую;
rn_extra - в release notes лишняя задача; rn_two - две строки на задачу, вторая с выбранным разделом; rn_code - в формулировке имя из кода;
recheck - агент просит второй круг; no_rules - агент не прочитал общие правила.
"""

import json
import re
import sys
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
prompt = sys.stdin.read()
item = re.search(r"^# Задание (\S+)", prompt, flags=re.M).group(1)
answer = Path(re.search(r"^Файл ответа: `([^`]+)`", prompt, flags=re.M).group(1))
code = re.search(r"`(R-[0-9a-f]{8})`", Path("AGENTS.md").read_text(encoding="utf-8")).group(1)

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
if mode == "no_evidence":
    ev = []
elif mode == "fake_quote":
    ev = [{"source": "changelog", "quote": "такой фразы в записях задачи нет"}]
elif mode == "foreign_source":
    ev = [{"source": "docs/MD_rus/docs/index.md", "quote": "Руководство"}]

data = {"item": item, "rules": "" if mode == "no_rules" else code, "status": "done",
        "summary": f"заглушка выполнила {item}",
        "steps": [] if mode == "no_steps" else [{"source": "docs/TODO.md" if mode == "foreign_step" else "changelog",
                                                  "found": "прочитал записи задачи"}],
        "evidence": ev, "questions": []}
with open("progress.md", "a", encoding="utf-8") as f:
    f.write(f"НАЧАЛ {item}\nГОТОВО {item}: done - заглушка\n")
if mode == "recheck" and not item.endswith("__round2") and item != "release_notes":
    data["status"] = "recheck"
    data["recheck_reason"] = "изменение можно понять двумя способами"
if "__decide" in item:
    data["decision"] = "rn" if "GUI-103" in item else "skip"
if "__write_doc" in item:
    pages = re.findall(r"^- `(docs/MD_[^`]+)`$", prompt, flags=re.M)
    page = pages[0]
    first = Path(page).read_text(encoding="utf-8").splitlines()[0]
    new = first + f"\n\nПравка заглушки {item}."
    if mode == "fake_term":
        new += "\nНажмите кнопку «Несуществующая кнопка»."
    if mode == "big_edit":
        new += "".join(f"\nЛишняя строка {n}." for n in range(40))
    if mode == "shot":
        new += '\n!!! warning "SCREENSHOT-TODO GUI-1"\n    Переснять: экран.'
    if mode == "comment":
        new += "\n<!-- TODO: уточнить у разработчиков -->"
    find = "такой строки на странице нет" if mode == "bad_find" else first
    if mode == "wrong_page":
        page = "docs/MD_rus/docs/index.md"
        find = Path(page).read_text(encoding="utf-8").splitlines()[0]
    if mode == "direct":
        other = Path("docs/MD_rus/docs/index.md")
        other.write_text(other.read_text(encoding="utf-8") + "\nПрямая правка мимо ответа.\n", encoding="utf-8")
    data["edits"] = [{"file": page, "find": find, "replace": new, "what": "добавлена строка", "evidence": ev}]
if item == "release_notes":
    tasks = re.findall(r"^### (\S+) \((.*)\)$", prompt, flags=re.M)
    lines = []
    for uid, state in tasks:
        if "решение принимаешь ты" in state and uid != "GUI-103":
            continue                          # заглушка решает skip по всем задачам, кроме GUI-103
        ru = f"Изменение по задаче {uid}." if mode != "rn_code" else "Переведено на resetFilters из `useFilters`."
        lines.append({"task": uid, "ru": ru, "en": f"Change for task {uid}."})
        if mode == "rn_two" and uid == "GUI-105":
            lines.append({"task": uid, "section": "Работа с системой", "ru": "Второе изменение по задаче.", "en": "Second change."})
    if mode == "rn_extra":
        lines.append({"task": "GUI-999", "ru": "Выдуманная строка.", "en": "Invented line."})
    data["lines"] = lines
answer.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"result": "ok"}))
