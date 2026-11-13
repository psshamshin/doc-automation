# Как проверить

Сбор не использует случайность, время и сеть: одинаковые данные и код дают одинаковые файлы.

## Без входных данных

```bash
python3 -m unittest discover -s tests            # в конце: OK
rm -rf output/demo && python3 -m pipeline collect tests/data/demo
shasum -a 256 output/demo/review.md output/demo/checks.md
```

Ожидается:

```
b08ca6d4f2467aefbe23b084faa19355d5a39ce5bdf94a53a1556a28a3dcbead  output/demo/review.md
d20fa47bee87777cb2484d32a8e4e69981af3643b638d5711ad015bfc3a49d7b  output/demo/checks.md
```

Те же файлы CI сохраняет как артефакт `demo-review`.

## На данных релиза 2.3.0

Данные в `input/` (без выгрузки Jira):

```bash
python3 -m pipeline versions                      # релиз 2.3.0, интерфейс 2.9.4 -> 2.11.0, документация 2.2.0
python3 -m pipeline collect --force
shasum -a 256 output/2.3.0/review.md output/2.3.0/checks.md
```

Ожидается:

```
45edfcce36b41898a42ade91967fef690c655f223e5e764ff1546563a0df73e7  output/2.3.0/review.md
473433631ea21766123b00c5ec077ceedc1f8bba28d1a3dff66feab5d586e003  output/2.3.0/checks.md
```

Задач 22; у 6 достоверность решения низкая. С отметками по умолчанию агент получит 15 заданий: 6 решений, 7 правок
документации, 1 проверку устаревших названий, 1 release notes (число может отличаться на 1-2 при другой выгрузке Jira).
Суммы верны для коммита, в котором менялся этот файл, и запуска без выгрузки Jira.

## Агент

`python3 -m pipeline agent --approve` готовит задания и ничего не вносит. Работу агента проверяет тот же код:
`python3 -m pipeline agent` после ответов. Тесты `tests/test_agent.py` проверяют это на заглушке вместо агента.
