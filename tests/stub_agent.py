"""Заглушка агента для тестов. Запускается в рабочей папке агента, читает задание из stdin, пишет ответ.

Режим задается первым параметром:
ok (по умолчанию) - правит первую страницу задания и подтверждает правку цитатой из записей задачи;
no_answer, bad_json, fail - сбои;
no_evidence - ответ без подтверждений; fake_quote - цитата, которой нет в источнике;
foreign_source - ссылка на файл вне разрешенных;
no_rules - агент не прочитал общие правила (нет кода из AGENTS.md);
edit_other - кроме своей страницы молча правит чужую; wrong_page - называет измененной чужую страницу.
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

data = {"item": item, "rules": "" if mode == "no_rules" else code, "status": "done", "summary": f"заглушка выполнила {item}", "log": ["прочитал записи задачи"],
        "evidence": ev, "questions": []}
if item.endswith("__decide"):
    data["decision"] = "rn" if "GUI-103" in item else "skip"
if item.endswith("__write_doc"):
    pages = re.findall(r"^- `(docs/MD_[^`]+)`$", prompt, flags=re.M)
    page = pages[0]
    p = Path(page)
    p.write_text(p.read_text(encoding="utf-8") + f"\nПравка заглушки {item}.\n", encoding="utf-8")
    declared = page
    if mode == "edit_other":
        other = Path("docs/MD_rus/docs/index.md")
        other.write_text(other.read_text(encoding="utf-8") + "\nЛишняя правка.\n", encoding="utf-8")
    if mode == "wrong_page":
        other = Path("docs/MD_rus/docs/index.md")
        other.write_text(other.read_text(encoding="utf-8") + "\nПравка не той страницы.\n", encoding="utf-8")
        declared = "docs/MD_rus/docs/index.md"
    with open("docs/trace.md", "a", encoding="utf-8") as f:
        f.write(f"| {item} | rus | {declared} | правка заглушки | Ж6 |\n")
    data["files_changed"] = [declared, "docs/trace.md"]
    data["edits"] = [{"file": declared, "what": "добавлена строка", "evidence": ev}]
answer.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"result": "ok"}))
