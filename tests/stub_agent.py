"""Заглушка агента для тестов: читает задание из stdin и пишет ответ в указанный файл.

Режим задается первым параметром: ok (по умолчанию), no_answer, bad_json, fail.
"""

import json
import re
import sys
from pathlib import Path

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
prompt = sys.stdin.read()
item = re.search(r"^# Задание (\S+)", prompt, flags=re.M).group(1)
answer = Path(re.search(r"Запиши его в файл `([^`]+)`", prompt).group(1))

if mode == "fail":
    sys.exit(3)
if mode == "no_answer":
    sys.exit(0)
answer.parent.mkdir(parents=True, exist_ok=True)
if mode == "bad_json":
    answer.write_text("не json", encoding="utf-8")
    sys.exit(0)
data = {"item": item, "status": "done", "summary": f"заглушка выполнила {item}", "evidence": [], "questions": []}
if item.endswith("__decide"):
    data["decision"] = "rn" if "GUI-103" in item else "skip"
answer.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"result": "ok"}))
