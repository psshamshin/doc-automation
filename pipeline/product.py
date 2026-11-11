"""То, что не меняется от релиза к релизу: имена папок, файлов и компонентов продукта.

Меняется продукт или устройство репозиториев - правится этот файл, а не настройки запуска.
"""

# ---- роли входных данных. Папки с этими именами создавать не нужно: пайплайн сам находит в наборе данных
# папки и архивы с кодом и документацией и по версиям определяет, что из них старое, а что новое.
SLOT_CODE_OLD = "source-code-old"            # исходный код фронтенда предыдущего релиза
SLOT_CODE_NEW = "source-code-new"            # исходный код фронтенда нового релиза
SLOT_DOCS_OLD = "docs-previous-release"      # документация предыдущего релиза
SLOT_DOCS_REFERENCE = "docs-reference"       # документация нового релиза от техписа (необязательно)
SLOT_JIRA = "jira-tasks"                     # выгрузка задач Jira (необязательно)

SLOTS = {
    SLOT_CODE_OLD: "исходный код фронтенда предыдущего релиза",
    SLOT_CODE_NEW: "исходный код фронтенда нового релиза",
    SLOT_DOCS_OLD: "документация предыдущего релиза",
    SLOT_DOCS_REFERENCE: "документация нового релиза от техписа, только для сравнения (необязательно)",
    SLOT_JIRA: "выгрузка задач Jira (необязательно)",
}
REQUIRED_SLOTS = [SLOT_CODE_OLD, SLOT_CODE_NEW, SLOT_DOCS_OLD]

# признак, по которому узнается корень папки (архив может распаковаться с лишним уровнем)
SLOT_MARKERS = {
    SLOT_CODE_OLD: "apps", SLOT_CODE_NEW: "apps",
    SLOT_DOCS_OLD: "docs/src", SLOT_DOCS_REFERENCE: "docs/src",
}

CODE_MARKER = "apps/point/package.json"     # признак папки с кодом фронтенда
DOCS_MARKER = "docs/src"                    # признак папки с документацией
UNPACKED_DIR = "_unpacked"                  # сюда распаковываются архивы из набора данных
ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar")
JIRA_SUFFIXES = (".xml", ".csv", ".json", ".txt")
SCAN_DEPTH = 3

# необязательный файл с переопределениями внутри набора данных
DATASET_FILE = "dataset.json"

# ---- код фронтенда
APP_PACKAGE_JSON = "apps/point/package.json"   # отсюда берется версия интерфейса
APP_CHANGELOG = "apps/point/CHANGELOG.md"

# ---- документация
DOC_LANGS = ["rus", "eng"]
RELEASE_NOTES_DIR = "release-notes"
UI_COMPONENT = "point-ui"                 # строка в таблице версий компонентов в release notes
UI_VERSION_VARIABLE = "SECURE_CLIENT_VER"  # переменная с версией интерфейса в руководстве администратора

RN_TITLES = {
    "rus": {"changes": "Изменения", "fixes": "Исправленные ошибки", "general": "Общее"},
    "eng": {"changes": "Changes", "fixes": "Fixes", "general": "General"},
}

# ---- база знаний (в git)
KNOWLEDGE_DIR = "knowledge"
RULES_FILE = "knowledge/rules.json"
DOC_MAP_FILE = "knowledge/doc_map.json"
RELEASES_FILE = "knowledge/releases.json"
LINKS_FILE = "knowledge/links.json"          # адреса внешних систем

# ---- папки проекта
INPUT_DIR = "input"
OUTPUT_DIR = "output"
RULES_DOC = "docs/RULES.md"

# ---- файлы результата внутри output/<набор>/
OUT_DATA = "data"            # промежуточные данные шага сбора
OUT_RESULT = "result"        # рабочая копия документации: сюда идут правки
OUT_AGENT = "agent"          # очередь, задания и ответы агента
OUT_REFERENCE = "reference"  # что изменил техпис (для сравнения)
OUT_COMPARE = "compare"
