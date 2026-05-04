"""Jargon dictionary — query expansion + LLM glossary block.

Loads `dictionary.json`, watches its mtime so admin edits take effect on
the next query (no restart). Matches whole words case-insensitively after
NFC normalisation, so "kex-jobb" and "KEX-jobb" hit the same entry whether
the source is a Swedish-decomposed filename or a precomposed user message.

Usage:
    j = Jargon.from_config(cfg)
    expanded, hits = j.expand_query("Hur fungerar KEX-jobb?", lang="sv")
    glossary_md = j.glossary_block(hits, lang="sv")
    note = j.transparency_note(hits, lang="sv")
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from student_bot.config import Config, get_config


# --- data ---


@dataclass
class JargonEntry:
    key: str             # lowercase NFC, used as dict key
    term: str            # display capitalisation
    expansion: str
    lang: str            # "sv" | "en" | "any"
    definition: str = ""
    added_by: str = ""
    added_ts: str = ""

    def matches_lang(self, query_lang: str) -> bool:
        if self.lang in ("any", ""):
            return True
        return self.lang == query_lang


def _nfc_lower(s: str) -> str:
    return unicodedata.normalize("NFC", s).lower()


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "entries": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# --- main class ---


class Jargon:
    """Loader + matcher. Use `from_config(cfg)`; never instantiate elsewhere."""

    def __init__(self, file_path: Path):
        self._path = file_path
        self._lock = Lock()
        self._mtime = 0.0
        self._entries: dict[str, JargonEntry] = {}
        self._pattern: re.Pattern | None = None
        self._reload_locked()

    @classmethod
    def from_config(cls, cfg: Config) -> "Jargon":
        path = cfg.absolute(Path(cfg.jargon.file))
        return cls(path)

    # --- (re)load on mtime change ---

    def _reload_if_changed(self) -> None:
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime if self._path.exists() else 0.0
            except OSError:
                mtime = 0.0
            if mtime != self._mtime:
                self._reload_locked()

    def _reload_locked(self) -> None:
        try:
            data = _read_json(self._path)
            entries_raw = data.get("entries", {})
        except (OSError, json.JSONDecodeError):
            entries_raw = {}
        entries: dict[str, JargonEntry] = {}
        for key, body in entries_raw.items():
            if not isinstance(body, dict):
                continue
            term = body.get("term") or key
            expansion = body.get("expansion", "")
            if not expansion:
                continue
            entries[_nfc_lower(key)] = JargonEntry(
                key=_nfc_lower(key),
                term=term,
                expansion=expansion,
                lang=body.get("lang", "any") or "any",
                definition=body.get("definition", "") or "",
                added_by=body.get("added_by", "") or "",
                added_ts=body.get("added_ts", "") or "",
            )
        self._entries = entries
        self._pattern = self._compile(entries)
        try:
            self._mtime = self._path.stat().st_mtime if self._path.exists() else 0.0
        except OSError:
            self._mtime = 0.0

    @staticmethod
    def _compile(entries: dict[str, JargonEntry]) -> re.Pattern | None:
        if not entries:
            return None
        # Longest-first so "kex-jobb" matches before a hypothetical "kex".
        keys = sorted(entries.keys(), key=len, reverse=True)
        alt = "|".join(re.escape(k) for k in keys)
        # \b on both sides — works for terms whose first/last char is a word
        # character. For terms ending in a non-word char we'd need a custom
        # boundary, but every seeded entry is fine.
        return re.compile(rf"\b({alt})\b", re.IGNORECASE | re.UNICODE)

    # --- public API ---

    def all_entries(self) -> list[JargonEntry]:
        self._reload_if_changed()
        return sorted(self._entries.values(), key=lambda e: e.key)

    def find(self, text: str, *, lang: str | None = None) -> list[JargonEntry]:
        """Unique entries hit by `text`, in order of first appearance."""
        self._reload_if_changed()
        if not text or not self._pattern:
            return []
        norm = unicodedata.normalize("NFC", text)
        seen: set[str] = set()
        hits: list[JargonEntry] = []
        for m in self._pattern.finditer(norm):
            key = _nfc_lower(m.group(1))
            entry = self._entries.get(key)
            if not entry or key in seen:
                continue
            if lang and not entry.matches_lang(lang):
                continue
            seen.add(key)
            hits.append(entry)
        return hits

    def expand_query(
        self, text: str, lang: str | None = None,
    ) -> tuple[str, list[JargonEntry]]:
        """Return (text-with-inline-expansions, hits). Each matched span is
        followed by " (<expansion>)". The original surface form is preserved."""
        self._reload_if_changed()
        if not text or not self._pattern:
            return text, []
        hits = self.find(text, lang=lang)
        if not hits:
            return text, []
        norm = unicodedata.normalize("NFC", text)
        emitted: set[str] = set()

        def repl(m: re.Match) -> str:
            key = _nfc_lower(m.group(1))
            entry = self._entries.get(key)
            if not entry:
                return m.group(0)
            if lang and not entry.matches_lang(lang):
                return m.group(0)
            # Only add the expansion the first time per query so we don't
            # spam if the user repeats the term.
            if key in emitted:
                return m.group(0)
            emitted.add(key)
            return f"{m.group(0)} ({entry.expansion})"

        expanded = self._pattern.sub(repl, norm)
        return expanded, hits

    def glossary_block(
        self, entries: list[JargonEntry], lang: str, max_entries: int = 6,
    ) -> str:
        """Markdown block for the LLM prompt. Empty string if no entries."""
        if not entries:
            return ""
        label = "Ordlista" if lang == "sv" else "Glossary"
        lines = [f"{label}:"]
        for e in entries[:max_entries]:
            base = f"- {e.term} = {e.expansion}"
            if e.definition:
                base += f". {e.definition}"
            lines.append(base)
        return "\n".join(lines)

    def transparency_note(
        self, entries: list[JargonEntry], lang: str,
    ) -> str:
        """One-line italic note shown above the answer. Empty if no hits."""
        if not entries:
            return ""
        if lang == "en":
            joined = ", ".join(f'"{e.term}" as "{e.expansion}"' for e in entries)
            return f"_Reading {joined}._"
        joined = ", ".join(f'"{e.term}" som "{e.expansion}"' for e in entries)
        return f"_Tolkar {joined}._"

    # --- mutators (used by the admin CLI and the suggestion endpoints) ---

    def add_entry(self, entry: JargonEntry) -> None:
        with self._lock:
            data = _read_json(self._path)
            entries = data.setdefault("entries", {})
            entries[entry.key] = {
                "term": entry.term,
                "expansion": entry.expansion,
                "lang": entry.lang,
                "definition": entry.definition,
                "added_by": entry.added_by,
                "added_ts": entry.added_ts,
            }
            data["version"] = data.get("version", 1)
            _write_json(self._path, data)
            self._reload_locked()

    def remove_entry(self, key: str) -> bool:
        norm_key = _nfc_lower(key)
        with self._lock:
            data = _read_json(self._path)
            entries = data.setdefault("entries", {})
            if norm_key not in entries:
                # Allow removal by `term` if user typed display form.
                for k, v in list(entries.items()):
                    if _nfc_lower(v.get("term", "")) == norm_key:
                        norm_key = k
                        break
                else:
                    return False
            del entries[norm_key]
            _write_json(self._path, data)
            self._reload_locked()
            return True


__all__ = ["JargonEntry", "Jargon", "_nfc_lower", "_read_json", "_write_json"]


if __name__ == "__main__":
    cfg = get_config()
    j = Jargon.from_config(cfg)
    if len(sys.argv) < 2:
        print(f"loaded {len(j.all_entries())} entries from {j._path}")
        for e in j.all_entries():
            print(f"  {e.term:<14} → {e.expansion}  [{e.lang}]")
        sys.exit(0)
    text = " ".join(sys.argv[1:])
    expanded, hits = j.expand_query(text, lang="sv")
    print(f"original  : {text}")
    print(f"expanded  : {expanded}")
    print("hits      :", [e.term for e in hits])
    print("note      :", j.transparency_note(hits, "sv"))
    print("glossary  :")
    print(j.glossary_block(hits, "sv"))
