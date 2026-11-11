"""Набор данных: где лежат входы и результаты и какие версии в них найдены.

Набор данных - одна папка, в которую сложены архивы или папки с кодом фронтенда двух версий,
документацией и, по желанию, выгрузкой Jira. Имена не важны: что есть что, определяется по содержимому,
что старое и что новое - по версиям внутри. Настраивать ничего не нужно.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import product as P
from . import releases as R
import shutil
import tarfile
import zipfile

from .common import ROOT, load_json, semver_key


class DatasetError(Exception):
    pass


def _safe_target(base: Path, name: str) -> Optional[Path]:
    t = (base / name).resolve()
    return t if base.resolve() in t.parents or t == base.resolve() else None


def unpack_archives(d: Path) -> List[str]:
    """Распаковывает архивы из набора данных в <набор>/_unpacked/<имя архива>/. Уже распакованные пропускает."""
    done = []
    for p in sorted(d.iterdir()):
        suf = next((x for x in P.ARCHIVE_SUFFIXES if p.name.lower().endswith(x)), None)
        if not p.is_file() or suf is None:
            continue
        target = d / P.UNPACKED_DIR / p.name[: -len(suf)]
        if target.is_dir():
            continue
        tmp = target.with_name(target.name + ".part")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        if suf == ".zip":
            with zipfile.ZipFile(p) as z:
                for m in z.infolist():
                    t = _safe_target(tmp, m.filename)
                    if t is None:
                        raise DatasetError(f"архив {p.name}: недопустимый путь внутри архива: {m.filename}")
                    if m.is_dir():
                        t.mkdir(parents=True, exist_ok=True)
                    else:
                        t.parent.mkdir(parents=True, exist_ok=True)
                        with z.open(m) as src, open(t, "wb") as dst:
                            shutil.copyfileobj(src, dst)
        else:
            with tarfile.open(p) as tf:
                for m in tf.getmembers():
                    if _safe_target(tmp, m.name) is None or m.issym() or m.islnk():
                        raise DatasetError(f"архив {p.name}: недопустимый путь или ссылка внутри архива: {m.name}")
                tf.extractall(tmp)
        tmp.rename(target)
        done.append(p.name)
    return done


def find_roots(d: Path) -> Dict[str, List[Path]]:
    """Ищет в наборе данных папки с кодом и документацией на глубину до SCAN_DEPTH."""
    found: Dict[str, List[Path]] = {"code": [], "docs": []}

    def walk(cur: Path, depth: int) -> None:
        if (cur / P.CODE_MARKER).is_file():
            found["code"].append(cur)
            return
        if (cur / P.DOCS_MARKER).is_dir():
            found["docs"].append(cur)
            return
        if depth >= P.SCAN_DEPTH:
            return
        for sub in sorted(cur.iterdir()):
            if sub.is_dir() and (not sub.name.startswith(".")) and sub.name != "node_modules":
                walk(sub, depth + 1)

    walk(d, 0)
    return found


class Dataset:
    def __init__(self, name_or_path: Optional[str] = None, out: Optional[str] = None, release: Optional[str] = None,
                 project_root: Path = ROOT):
        self.base = project_root
        inbox = self.base / P.INPUT_DIR
        if name_or_path is None:
            p = self._default_dir(inbox)
        else:
            p = Path(name_or_path)
            if not p.is_dir():
                p = inbox / name_or_path
            if not p.is_dir():
                raise DatasetError(f"набор данных не найден: {name_or_path} (ожидалась папка {p})")
        self.dir = p.resolve()
        self.unpacked = unpack_archives(self.dir)
        ov = self.dir / P.DATASET_FILE
        self.overrides: Dict[str, Any] = load_json(ov) if ov.is_file() else {}
        self.rules: Dict[str, Any] = load_json(self.base / P.RULES_FILE)
        dm = self.overrides.get("doc_map")
        self.doc_map: Dict[str, Any] = load_json(self.dir / dm if dm else self.base / P.DOC_MAP_FILE)
        self.knowledge_path = self.base / P.RELEASES_FILE
        self.langs: List[str] = list(P.DOC_LANGS)
        self._release_arg = release
        self.roles = self._assign_roles()
        self.versions = self._detect_versions()
        # папка input/ без вложенных наборов: результаты называются по версии релиза
        self.name = self.versions["release"] if self.dir == inbox.resolve() else self.dir.name
        self.run_dir = Path(out).resolve() if out else self.base / P.OUTPUT_DIR / self.name

    @staticmethod
    def _default_dir(inbox: Path) -> Path:
        """Без имени набора: сама папка input/, если данные лежат прямо в ней, иначе единственная вложенная папка."""
        if not inbox.is_dir():
            raise DatasetError(f"нет папки {inbox}")
        has_archives = any(p.is_file() and p.name.lower().endswith(P.ARCHIVE_SUFFIXES) for p in inbox.iterdir())
        subs = [p for p in sorted(inbox.iterdir()) if p.is_dir() and not p.name.startswith((".", "_"))]
        direct = has_archives or any((s / P.CODE_MARKER).is_file() or (s / P.DOCS_MARKER).is_dir() for s in subs)
        if direct:
            return inbox
        if len(subs) == 1:
            return subs[0]
        if not subs:
            raise DatasetError(f"папка {inbox} пуста: положите в нее архивы с кодом и документацией")
        raise DatasetError("в input/ несколько наборов данных, укажите имя: " + ", ".join(s.name for s in subs))

    def _assign_roles(self) -> Dict[str, Optional[Path]]:
        """Что из найденного старое, а что новое, решают версии внутри данных, а не имена папок."""
        found = find_roots(self.dir)
        codes = sorted(((semver_key(R.ui_version_of(c) or "0"), c) for c in found["code"]), key=lambda x: x[0])
        docs = []
        for dd in found["docs"]:
            hist = R.docs_releases(dd)
            docs.append((semver_key(hist[0]["version"]) if hist else (0, 0, 0), dd))
        docs.sort(key=lambda x: x[0])

        def names(items):
            return ", ".join(f"{p.relative_to(self.dir)} ({'.'.join(map(str, k))})" for k, p in items)

        if len(codes) != 2 or codes[0][0] == codes[1][0]:
            raise DatasetError(
                f"в наборе данных {self.dir.name} нужно ровно два варианта кода фронтенда разных версий "
                f"(предыдущий релиз и новый), найдено {len(codes)}: {names(codes) or 'ничего'}. "
                f"Код узнается по файлу {P.CODE_MARKER}")
        if len(docs) not in (1, 2) or (len(docs) == 2 and docs[0][0] == docs[1][0]):
            raise DatasetError(
                f"в наборе данных {self.dir.name} нужна документация предыдущего релиза и, по желанию, нового "
                f"(для сравнения), найдено {len(docs)}: {names(docs) or 'ничего'}. "
                f"Документация узнается по папке {P.DOCS_MARKER}")
        return {P.SLOT_CODE_OLD: codes[0][1], P.SLOT_CODE_NEW: codes[1][1], P.SLOT_DOCS_OLD: docs[0][1],
                P.SLOT_DOCS_REFERENCE: docs[1][1] if len(docs) == 2 else None}

    # ---- пути
    def slot(self, slot: str) -> Path:
        p = self.roles.get(slot)
        if p is None:
            raise DatasetError(f"в наборе данных нет: {P.SLOTS[slot]}")
        return p

    def has(self, slot: str) -> bool:
        return self.roles.get(slot) is not None

    def jira_files(self) -> List[Path]:
        """Выгрузки Jira: файлы в корне набора данных и в папках, имя которых начинается с 'jira'."""
        out = []
        for p in sorted(self.dir.iterdir()):
            if p.is_file() and p.name not in (P.DATASET_FILE, self.overrides.get("doc_map")) \
                    and p.suffix.lower() in P.JIRA_SUFFIXES:
                out.append(p)
            elif p.is_dir() and p.name.lower().startswith("jira"):
                out += [f for f in sorted(p.iterdir()) if f.is_file() and f.suffix.lower() in P.JIRA_SUFFIXES + (".md",)]
        return out

    @property
    def data_dir(self) -> Path:
        return self.run_dir / P.OUT_DATA

    @property
    def result_dir(self) -> Path:
        return self.run_dir / P.OUT_RESULT

    @property
    def agent_dir(self) -> Path:
        return self.run_dir / P.OUT_AGENT

    def rel(self, p) -> str:
        """Путь относительно проекта: результаты не зависят от того, где лежит проект."""
        p = Path(p).resolve()
        try:
            return p.relative_to(self.base.resolve()).as_posix()
        except ValueError:
            return p.as_posix()

    # ---- версии
    @property
    def release(self) -> str:
        return self.versions["release"]

    def _detect_versions(self) -> Dict[str, Any]:
        notes: List[str] = []
        ui_old = R.ui_version_of(self.slot(P.SLOT_CODE_OLD))
        ui_new = R.ui_version_of(self.slot(P.SLOT_CODE_NEW))
        if not ui_old or not ui_new:
            raise DatasetError(f"не удалось определить версию интерфейса: нет {P.APP_PACKAGE_JSON} в папках с кодом")

        hist = R.docs_releases(self.slot(P.SLOT_DOCS_OLD))
        base = hist[0] if hist else None
        docs_base = base["version"] if base else None
        if base is None:
            notes.append("в документации предыдущего релиза не найден файл release notes с заголовком '# [x.y.z]'")
        elif base["components"].get(P.UI_COMPONENT) not in (None, ui_old):
            notes.append(f"в release notes {docs_base} указан {P.UI_COMPONENT} {base['components'][P.UI_COMPONENT]}, "
                         f"а в папке {P.SLOT_CODE_OLD} лежит код {ui_old}: проверьте, что код и дока от одного релиза")

        kn = R.load_knowledge(self.knowledge_path)
        ref = None
        if self.has(P.SLOT_DOCS_REFERENCE):
            rh = R.docs_releases(self.slot(P.SLOT_DOCS_REFERENCE))
            ref = rh[0] if rh and (not docs_base or rh[0]["version"] != docs_base) else None
        known = R.find_by_ui(kn, ui_new)

        if self._release_arg:
            release, source = self._release_arg, "параметр --release"
        elif self.overrides.get("release"):
            release, source = self.overrides["release"], f"файл {P.DATASET_FILE} в наборе данных"
        elif known:
            release, source = known["release"], f"база знаний: {P.UI_COMPONENT} {ui_new} = релиз {known['release']}"
        elif ref:
            release, source = ref["version"], "release notes в документации техписа"
        elif docs_base:
            release, source = R.next_minor(docs_base), "догадка: следующая версия после документации предыдущего релиза"
            notes.append(f"версия нового релиза угадана ({release}). Если она другая, запустите с --release <версия>: "
                         "она запомнится в базе знаний")
        else:
            raise DatasetError("не удалось определить версию нового релиза: укажите --release <версия>")

        rec = R.find_release(kn, release) or {}
        if rec.get("ui") not in (None, ui_new):
            notes.append(f"в базе знаний релиз {release} записан с {P.UI_COMPONENT} {rec['ui']}, а в папке {P.SLOT_CODE_NEW} "
                         f"лежит код {ui_new}: запись базы знаний не использована, проверьте версию релиза")
            rec = {}
        date = self.overrides.get("release_date") or rec.get("date") or (ref["date"] if ref and ref["version"] == release else None)
        if not date:
            notes.append("дата релиза неизвестна: в черновике release notes стоит ДД.ММ.ГГГГ")
        return {
            "release": release, "release_source": source, "release_date": date,
            "docs_base_version": docs_base, "ui_old": ui_old, "ui_new": ui_new,
            "release_notes_file": R.release_notes_filename(release),
            "previous_release_notes_file": base["file"] if base else None,
            "previous_components": base["components"] if base else {},
            "notes": notes,
        }
