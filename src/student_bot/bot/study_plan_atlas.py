"""Loader for the curated study-plan topic atlas (`data/study_plan_atlas.yaml`).

The atlas declares which `studyProgramme.*` fields and which sidebar pages
plausibly carry each topic (e.g. master-program lists live in
`arskursinformationAr4` for CTFYS but in `utbildningensupplagg` for CINEK).
This module exposes the YAML as Python data and a forward field→topic
inverse map used by the per-section chunker in ``web_retrieval``.

Adding a topic = edit the YAML; no code change required.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from student_bot.config import Config

log = logging.getLogger("student_bot")


@dataclass
class Topic:
    name: str
    label_sv: str = ""
    label_en: str = ""
    look_in: list[str] = field(default_factory=list)
    notes: str = ""
    label_sv_pattern: str = ""
    label_en_pattern: str = ""

    def label(self, lang: str) -> str:
        if lang == "en" and self.label_en:
            return self.label_en
        return self.label_sv or self.label_en or self.name


@dataclass
class ValvillkorLabel:
    label_sv: str
    label_en: str

    def label(self, lang: str) -> str:
        return self.label_en if lang == "en" else self.label_sv


@dataclass
class Atlas:
    topics: dict[str, Topic]
    valvillkor: dict[str, ValvillkorLabel]
    # Inverse map: studyProgramme field name → (topic_name, label_sv, label_en).
    # Built once at load. Multiple topics may claim the same field — first
    # wins (atlas iteration order).
    field_to_topic: dict[str, str]

    def topic_label(self, topic_name: str, lang: str) -> str:
        t = self.topics.get(topic_name)
        return t.label(lang) if t else topic_name

    def label_for_field(self, field_name: str, lang: str) -> str | None:
        topic_name = self.field_to_topic.get(field_name)
        if not topic_name:
            return None
        return self.topic_label(topic_name, lang)

    def valvillkor_label(self, code: str, lang: str) -> str:
        v = self.valvillkor.get(code)
        return v.label(lang) if v else code


_ATLAS_CACHE: dict[str, tuple[float, Atlas]] = {}
_ATLAS_TTL_SECONDS = 300.0  # mtime check cadence; the file rarely changes


_DEFAULT_ATLAS_PATH = "data/study_plan_atlas.yaml"


def _empty_atlas() -> Atlas:
    return Atlas(topics={}, valvillkor={}, field_to_topic={})


def _build_atlas_from_data(data: dict[str, Any]) -> Atlas:
    topics_raw = data.get("topics", {}) if isinstance(data, dict) else {}
    valv_raw = data.get("valvillkor_labels", {}) if isinstance(data, dict) else {}

    topics: dict[str, Topic] = {}
    # field -> (best_position_in_look_in, topic_name). A field's primary topic
    # is the one where it appears earliest in `look_in` — ties broken by
    # YAML declaration order. This way `utbildningensupplagg` resolves to
    # `structure` (where it's first) rather than `master_programs` (where it
    # appears as a tertiary fallback).
    best: dict[str, tuple[int, str]] = {}
    for name, body in (topics_raw or {}).items():
        if not isinstance(body, dict):
            continue
        t = Topic(
            name=name,
            label_sv=str(body.get("label_sv", "") or ""),
            label_en=str(body.get("label_en", "") or ""),
            look_in=[str(x) for x in (body.get("look_in") or []) if isinstance(x, str)],
            notes=str(body.get("notes", "") or ""),
            label_sv_pattern=str(body.get("label_sv_pattern", "") or ""),
            label_en_pattern=str(body.get("label_en_pattern", "") or ""),
        )
        topics[name] = t
        for idx, entry in enumerate(t.look_in):
            if not entry.startswith("studyProgramme."):
                continue
            key = entry[len("studyProgramme.") :]
            prev = best.get(key)
            if prev is None or idx < prev[0]:
                best[key] = (idx, name)
    field_to_topic: dict[str, str] = {k: v[1] for k, v in best.items()}

    valvillkor: dict[str, ValvillkorLabel] = {}
    for code, body in (valv_raw or {}).items():
        if not isinstance(body, dict):
            continue
        valvillkor[str(code)] = ValvillkorLabel(
            label_sv=str(body.get("label_sv", "") or ""),
            label_en=str(body.get("label_en", "") or ""),
        )

    return Atlas(topics=topics, valvillkor=valvillkor, field_to_topic=field_to_topic)


def _load_atlas_file(path: Path) -> Atlas:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("study-plan atlas not found at %s; using empty atlas", path)
        return _empty_atlas()
    except (OSError, yaml.YAMLError) as e:
        log.warning("study-plan atlas load failed for %s: %s", path, e)
        return _empty_atlas()
    if not isinstance(data, dict):
        return _empty_atlas()
    return _build_atlas_from_data(data)


def get_atlas(cfg: Config | None = None) -> Atlas:
    """Return the cached atlas, refreshing only when the file mtime changes."""
    if cfg is not None:
        path = cfg.absolute(Path(_DEFAULT_ATLAS_PATH))
    else:
        path = Path(_DEFAULT_ATLAS_PATH).resolve()
    key = str(path)
    now = time.time()
    cached = _ATLAS_CACHE.get(key)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    if cached and now - cached[0] < _ATLAS_TTL_SECONDS:
        # Even when fresh by TTL, re-read if the file was edited.
        cached_atlas = cached[1]
        if mtime and getattr(cached_atlas, "_mtime", None) == mtime:
            return cached_atlas
    atlas = _load_atlas_file(path)
    setattr(atlas, "_mtime", mtime)
    _ATLAS_CACHE[key] = (now, atlas)
    return atlas


__all__ = ["Atlas", "Topic", "ValvillkorLabel", "get_atlas"]
