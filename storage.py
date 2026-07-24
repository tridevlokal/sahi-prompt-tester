"""
storage.py — JSON-file persistence for saved prompts and voices.

Two JSON files in ./data/:
  data/prompts.json -> {"prompts": [...]}
  data/voices.json  -> {"voices":  [...]}

Each entry has: id, name, type-specific fields, created_at, updated_at.
Files are auto-created on first read. voices.json is seeded from .env
VOICE_ID_* variables when it doesn't yet exist.

Convention: `entry_id` is the saved row's internal id. For voices,
`voice_id` separately means the ElevenLabs voice identifier.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
PROMPTS_FILE = DATA_DIR / "prompts.json"
VOICES_FILE = DATA_DIR / "voices.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _read(path: Path, key: str, default_seed: list[dict] | None = None) -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if not path.exists():
        seed = {key: default_seed or []}
        path.write_text(json.dumps(seed, indent=2))
        return seed
    return json.loads(path.read_text())


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _seed_voices_from_env() -> list[dict]:
    languages = ["english", "tamil", "telugu", "kannada"]
    voices = []
    for lang in languages:
        voice_id = os.getenv(f"VOICE_ID_{lang.upper()}", "")
        if not voice_id:
            continue
        voices.append({
            "id": _new_id(),
            "name": f"Riya {lang.title()}",
            "voice_id": voice_id,
            "language": lang,
            "tags": [],
            "notes": "Auto-seeded from .env on first run",
            "created_at": _now(),
            "updated_at": _now(),
        })
    return voices


# ----- prompts CRUD -----

def list_prompts() -> list[dict]:
    return _read(PROMPTS_FILE, "prompts").get("prompts", [])


def get_prompt(entry_id: str) -> dict | None:
    return next((p for p in list_prompts() if p["id"] == entry_id), None)


def create_prompt(*, name: str, system_prompt: str,
                  tags: list[str] | None = None,
                  notes: str = "") -> dict:
    data = _read(PROMPTS_FILE, "prompts")
    entry = {
        "id": _new_id(),
        "name": name,
        "system_prompt": system_prompt,
        "tags": tags or [],
        "notes": notes,
        "created_at": _now(),
        "updated_at": _now(),
    }
    data["prompts"].append(entry)
    _write(PROMPTS_FILE, data)
    return entry


def update_prompt(entry_id: str, **fields: Any) -> dict | None:
    editable = {"name", "system_prompt", "tags", "notes"}
    data = _read(PROMPTS_FILE, "prompts")
    for p in data["prompts"]:
        if p["id"] == entry_id:
            for k, v in fields.items():
                if k in editable:
                    p[k] = v
            p["updated_at"] = _now()
            _write(PROMPTS_FILE, data)
            return p
    return None


def delete_prompt(entry_id: str) -> bool:
    data = _read(PROMPTS_FILE, "prompts")
    before = len(data["prompts"])
    data["prompts"] = [p for p in data["prompts"] if p["id"] != entry_id]
    if len(data["prompts"]) == before:
        return False
    _write(PROMPTS_FILE, data)
    return True


# ----- voices CRUD -----

def list_voices() -> list[dict]:
    return _read(VOICES_FILE, "voices",
                 default_seed=_seed_voices_from_env()).get("voices", [])


def get_voice(entry_id: str) -> dict | None:
    return next((v for v in list_voices() if v["id"] == entry_id), None)


def create_voice(*, name: str, voice_id: str,
                 language: str = "",
                 tags: list[str] | None = None,
                 notes: str = "") -> dict:
    data = _read(VOICES_FILE, "voices",
                 default_seed=_seed_voices_from_env())
    entry = {
        "id": _new_id(),
        "name": name,
        "voice_id": voice_id,
        "language": language,
        "tags": tags or [],
        "notes": notes,
        "created_at": _now(),
        "updated_at": _now(),
    }
    data["voices"].append(entry)
    _write(VOICES_FILE, data)
    return entry


def update_voice(entry_id: str, **fields: Any) -> dict | None:
    editable = {"name", "voice_id", "language", "tags", "notes"}
    data = _read(VOICES_FILE, "voices",
                 default_seed=_seed_voices_from_env())
    for v in data["voices"]:
        if v["id"] == entry_id:
            for k, val in fields.items():
                if k in editable:
                    v[k] = val
            v["updated_at"] = _now()
            _write(VOICES_FILE, data)
            return v
    return None


def delete_voice(entry_id: str) -> bool:
    data = _read(VOICES_FILE, "voices",
                 default_seed=_seed_voices_from_env())
    before = len(data["voices"])
    data["voices"] = [v for v in data["voices"] if v["id"] != entry_id]
    if len(data["voices"]) == before:
        return False
    _write(VOICES_FILE, data)
    return True
