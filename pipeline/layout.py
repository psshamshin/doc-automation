"""Набор данных: где лежат входы и результаты и какие версии в них найдены.

Набор данных - папка input/<имя>/ с подпапками из product.SLOTS. Настраивать ничего не нужно:
версии читаются из самих данных и из базы знаний knowledge/releases.json.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import product as P
from . import releases as R
from .common import ROOT, load_json


class DatasetError(Exception):
    pass


def _slot_root(d: Path, marker: Optional[str]) -> Path:
    """Если архив распаковался с лишним уровнем (input/x/source-code-new/lerna-.../apps), спускаемся в него."""
    if marker is None or (d / marker).exists() or not d.is_dir():
        return d
    subs = [p for p in sorted(d.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    if len(subs) == 1 and (subs[0] / marker).exists():
        return subs[0]
    return d


class Dataset:
    def __init__(self, name_or_path: str, out: Optional[str] = None, release: Optional[str] = None,
                 project_root: Path = ROOT):
        self.base = project_root
        p = Path(name_or_path)
        if not p.is_dir():
            p = self.base / P.INPUT_DIR / name_or_path
        if not p.is_dir():
            raise DatasetError(f"набор данных не найден: {name_or_path} (ожидалась папка {p})")
        self.dir = p.resolve()
        self.name = self.dir.name
        self.run_dir = Path(out).resolve() if out else self.base / P.OUTPUT_DIR / self.name
        ov = self.dir / P.DATASET_FILE
        self.overrides: Dict[str, Any] = load_json(ov) if ov.is_file() else {}
        self.rules: Dict[str, Any] = load_json(self.base / P.RULES_FILE)
        dm = self.overrides.get("doc_map")
        self.doc_map: Dict[str, Any] = load_json(self.dir / dm if dm else self.base / P.DOC_MAP_FILE)
        self.knowledge_path = self.base / P.RELEASES_FILE
        self.langs: List[str] = list(P.DOC_LANGS)
        self._release_arg = release
        missing = [s for s in P.REQUIRED_SLOTS if not self.slot(s).is_dir()]
        if missing:
            raise DatasetError("в наборе данных нет обязательных папок: " + ", ".join(
                f"{self.dir.name}/{s} ({P.SLOTS[s]})" for s in missing))
        self.versions = self._detect_versions()

    # ---- пути
    def slot(self, slot: str) -> Path:
        return _slot_root(self.dir / slot, P.SLOT_MARKERS.get(slot))

    def has(self, slot: str) -> bool:
        return self.slot(slot).is_dir() and any(self.slot(slot).iterdir())

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
            "reference_ignore": self.overrides.get("reference_ignore", rec.get("reference_ignore", [])),
            "notes": notes,
        }
