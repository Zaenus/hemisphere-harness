"""Memory across conversations: short notes the agent keeps, in a local file. Off by default.

At the end of a run the right decides whether anything is worth remembering beyond this
conversation, and of what kind. Usually nothing is.

    preference   how the person likes to be answered or worked with. Applies to every
                 request, so it is always recalled.
    fact         something lasting about them, their work or their situation. Matters only
                 when a request touches it, so the left decides, note by note, which to recall.

    HARNESS_MEMORY=true
    HARNESS_MEMORY_FILE=.hemisphere/memory.json     (the default)

Notes stay on this machine, in that file. Delete it, or individual notes in the web UI's
settings, to forget.
"""

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["preference", "fact"]

DEFAULT_FILE = Path(".hemisphere") / "memory.json"
MAX_NOTES = 200
"""The oldest notes are dropped beyond this."""
MAX_NOTE_CHARS = 400


class Note(BaseModel):
    id: str
    text: str
    kind: Kind = "fact"
    created: datetime
    source: str = ""
    """What led to it, e.g. the request it came from."""


class MemoryStore:
    """Notes in a JSON file. Reads the file on every access, so several runs can share it."""

    def __init__(self, path: str | os.PathLike[str] = DEFAULT_FILE) -> None:
        self.path = Path(path).expanduser()

    @property
    def notes(self) -> list[Note]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Note.model_validate(item) for item in data.get("notes", [])]

    def add(self, items: "list[str] | list[Memo]", *, source: str = "") -> list[Note]:
        """Save new notes, skipping any the store already holds. Returns those it saved.

        Plain strings are saved as facts.
        """
        notes = self.notes
        known = {note.text.strip().lower() for note in notes}
        added = []
        for item in items:
            memo = item if isinstance(item, Memo) else Memo(text=item)
            text = " ".join(memo.text.split())[:MAX_NOTE_CHARS]
            if not text or text.lower() in known:
                continue
            known.add(text.lower())
            note = Note(id=secrets.token_hex(4), text=text, kind=memo.kind, created=datetime.now(UTC), source=source[:200])
            notes.append(note)
            added.append(note)
        if added:
            self._write(notes[-MAX_NOTES:])
        return added

    def delete(self, note_id: str) -> bool:
        notes = self.notes
        kept = [note for note in notes if note.id != note_id]
        if len(kept) == len(notes):
            return False
        self._write(kept)
        return True

    def clear(self) -> None:
        self._write([])

    def _write(self, notes: list[Note]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_suffix(".json.partial")
        partial.write_text(json.dumps({"notes": [n.model_dump(mode="json") for n in notes]}, indent=1), encoding="utf-8")
        os.chmod(partial, 0o600)
        os.replace(partial, self.path)


class Memo(BaseModel):
    """One thing the right decides to keep."""

    text: str
    kind: Kind = "fact"


class Remembered(BaseModel):
    """What the right decides to keep from a run."""

    notes: list[Memo] = Field(default_factory=list)
