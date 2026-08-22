"""Persistent, reviewable pronunciation library and deterministic lookup."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from sqlalchemy import or_, select

from .database import Database
from .models import PronunciationEntry, SessionRecord, utcnow


RESPELLING_RE = re.compile(r"^[a-z]+(?:-[a-z]+)*(?: [a-z]+(?:-[a-z]+)*)*$")
VALID_SCOPES = {"global", "session"}
VALID_STATUSES = {"proposed", "reviewed", "disabled"}
VALID_ALPHABETS = {"respelling"}


def normalize_source_form(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.casefold().split())


def normalize_language(value: object) -> str:
    normalized = str(value or "und").strip().replace("_", "-").lower()
    return normalized or "und"


def normalize_backend(value: object) -> str:
    return str(value or "*").strip().lower().replace(" ", "_") or "*"


def validate_respelling(value: object) -> str:
    phonetic = " ".join(str(value or "").strip().split())
    if not RESPELLING_RE.fullmatch(phonetic):
        raise ValueError(
            "Pronunciation must use lowercase ASCII syllables separated by hyphens "
            "(for example, ee-mah-oh-kah)."
        )
    return phonetic


def render_respelling(value: object) -> str:
    """Render the controlled respelling format for the current plain-text backends."""
    return validate_respelling(value).replace("-", "")


def _bounded_pattern(term: str) -> str:
    """Build a case-insensitive, word-bounded pattern for a written form.

    Written forms are stored with collapsed whitespace, but speech text may
    contain line breaks or repeated spaces.  Treating whitespace runs as one
    separator keeps lookup bounded without making the library mutate source
    text before it reaches the TTS provider.
    """
    normalized = " ".join(str(term or "").split())
    words = normalized.split(" ") if normalized else []
    if not words:
        return r"(?!)"
    escaped = r"\s+".join(re.escape(word) for word in words)
    prefix = r"(?<!\w)" if words[0][0].isalnum() else ""
    suffix = r"(?!\w)" if words[-1][-1].isalnum() else ""
    return prefix + escaped + suffix


def _bounded_contains(text: str, term: str) -> bool:
    return re.search(_bounded_pattern(term), text, flags=re.IGNORECASE) is not None


def apply_reviewed_pronunciations(
    text: str,
    entries: list[dict[str, Any]],
) -> str:
    """Replace selected library entries with their controlled respellings.

    ``resolve`` has already applied scope/language/backend precedence.  This
    second, pure pass deliberately works on payloads so callers can use it
    without holding ORM objects or mutating library records.  Matches are
    selected longest-first and never overlap; punctuation and all unmatched
    source text are preserved exactly.
    """
    if not text or not entries:
        return text

    candidates: list[tuple[int, int, int, str]] = []
    for rank, entry in enumerate(entries):
        source_form = str(entry.get("source_form") or "").strip()
        if not source_form:
            continue
        phonetic = entry.get("phonetic")
        try:
            replacement = render_respelling(phonetic)
        except ValueError:
            # Database-created entries are validated, but an imported or old
            # payload must never make synthesis fail unexpectedly.
            continue
        for match in re.finditer(
            _bounded_pattern(source_form), text, flags=re.IGNORECASE
        ):
            candidates.append((match.start(), match.end(), rank, replacement))

    selected: list[tuple[int, int, str]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, rank, replacement in sorted(
        candidates,
        key=lambda item: (-(item[1] - item[0]), item[2], item[0]),
    ):
        if any(start < other_end and end > other_start for other_start, other_end in occupied):
            continue
        occupied.append((start, end))
        selected.append((start, end, replacement))

    for start, end, replacement in sorted(selected, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


class PronunciationLibrary:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def payload(
        entry: PronunciationEntry,
        *,
        session_name: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": entry.id,
            "scope": entry.scope,
            "session_id": entry.session_id,
            "session_name": session_name,
            "source_form": entry.source_form,
            "language": entry.language,
            "phonetic": entry.phonetic,
            "alphabet": entry.alphabet,
            "backend": entry.backend,
            "status": entry.status,
            "source": entry.source,
            "notes": entry.notes,
            "metadata": dict(entry.metadata_json or {}),
            "revision": entry.revision,
            "created_at": entry.created_at.isoformat(),
            "updated_at": entry.updated_at.isoformat(),
        }

    @staticmethod
    def _normalized_values(values: dict[str, Any]) -> dict[str, Any]:
        source_form = " ".join(str(values.get("source_form") or "").split())
        if not source_form:
            raise ValueError("Written form is required.")
        if len(source_form) > 512:
            raise ValueError("Written form may not exceed 512 characters.")
        scope = str(values.get("scope") or "global").strip().lower()
        if scope not in VALID_SCOPES:
            raise ValueError("Pronunciation scope must be global or session.")
        session_id = str(values.get("session_id") or "").strip() or None
        if scope == "session" and not session_id:
            raise ValueError("A session-scoped pronunciation requires a session.")
        if scope == "global":
            session_id = None
        status = str(values.get("status") or "reviewed").strip().lower()
        if status not in VALID_STATUSES:
            raise ValueError("Pronunciation status must be proposed, reviewed, or disabled.")
        alphabet = str(values.get("alphabet") or "respelling").strip().lower()
        if alphabet not in VALID_ALPHABETS:
            raise ValueError("Only the structured respelling alphabet is currently supported.")
        notes = str(values.get("notes") or "").strip() or None
        return {
            "scope": scope,
            "session_id": session_id,
            "source_form": source_form,
            "normalized_form": normalize_source_form(source_form),
            "language": normalize_language(values.get("language")),
            "phonetic": validate_respelling(values.get("phonetic")),
            "alphabet": alphabet,
            "backend": normalize_backend(values.get("backend")),
            "status": status,
            "source": str(values.get("source") or "manual").strip().lower() or "manual",
            "notes": notes,
            "metadata_json": dict(values.get("metadata") or values.get("metadata_json") or {}),
        }

    @staticmethod
    def _duplicate(
        session,
        values: dict[str, Any],
        *,
        excluding_id: str | None = None,
    ) -> PronunciationEntry | None:
        statement = select(PronunciationEntry).where(
            PronunciationEntry.scope == values["scope"],
            PronunciationEntry.normalized_form == values["normalized_form"],
            PronunciationEntry.language == values["language"],
            PronunciationEntry.backend == values["backend"],
        )
        if values["session_id"] is None:
            statement = statement.where(PronunciationEntry.session_id.is_(None))
        else:
            statement = statement.where(
                PronunciationEntry.session_id == values["session_id"]
            )
        if excluding_id:
            statement = statement.where(PronunciationEntry.id != excluding_id)
        return session.scalar(statement)

    def list(
        self,
        *,
        query: str = "",
        language: str = "",
        status: str = "",
        scope: str = "",
        session_id: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            statement = select(PronunciationEntry).order_by(
                PronunciationEntry.status != "proposed",
                PronunciationEntry.updated_at.desc(),
                PronunciationEntry.source_form,
            )
            if query.strip():
                pattern = f"%{query.strip()}%"
                statement = statement.where(
                    or_(
                        PronunciationEntry.source_form.ilike(pattern),
                        PronunciationEntry.phonetic.ilike(pattern),
                        PronunciationEntry.notes.ilike(pattern),
                    )
                )
            if language.strip():
                statement = statement.where(
                    PronunciationEntry.language == normalize_language(language)
                )
            if status.strip():
                statement = statement.where(
                    PronunciationEntry.status == status.strip().lower()
                )
            if scope.strip():
                statement = statement.where(
                    PronunciationEntry.scope == scope.strip().lower()
                )
            if session_id.strip():
                statement = statement.where(
                    PronunciationEntry.session_id == session_id.strip()
                )
            entries = list(session.scalars(statement.limit(max(1, min(limit, 1000)))).all())
            session_ids = {item.session_id for item in entries if item.session_id}
            session_names = (
                {
                    record.id: record.name
                    for record in session.scalars(
                        select(SessionRecord).where(SessionRecord.id.in_(session_ids))
                    ).all()
                }
                if session_ids
                else {}
            )
            return [
                self.payload(item, session_name=session_names.get(item.session_id))
                for item in entries
            ]

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalized_values(values)
        with self.database.session() as session:
            if normalized["session_id"] and session.get(
                SessionRecord, normalized["session_id"]
            ) is None:
                raise KeyError(normalized["session_id"])
            if self._duplicate(session, normalized):
                raise ValueError(
                    "A pronunciation for this written form, language, backend, and scope already exists."
                )
            entry = PronunciationEntry(**normalized)
            session.add(entry)
            session.flush()
            return self.payload(entry)

    def update(
        self,
        entry_id: str,
        expected_revision: int,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        with self.database.session() as session:
            entry = session.get(PronunciationEntry, entry_id)
            if entry is None:
                raise KeyError(entry_id)
            if entry.revision != expected_revision:
                raise ValueError("The pronunciation changed in another client.")
            current = {
                "scope": entry.scope,
                "session_id": entry.session_id,
                "source_form": entry.source_form,
                "language": entry.language,
                "phonetic": entry.phonetic,
                "alphabet": entry.alphabet,
                "backend": entry.backend,
                "status": entry.status,
                "source": entry.source,
                "notes": entry.notes,
                "metadata": dict(entry.metadata_json or {}),
            }
            normalized = self._normalized_values({**current, **changes})
            if normalized["session_id"] and session.get(
                SessionRecord, normalized["session_id"]
            ) is None:
                raise KeyError(normalized["session_id"])
            if self._duplicate(session, normalized, excluding_id=entry.id):
                raise ValueError(
                    "A pronunciation for this written form, language, backend, and scope already exists."
                )
            for key, value in normalized.items():
                setattr(entry, key, value)
            entry.revision += 1
            entry.updated_at = utcnow()
            session.flush()
            return self.payload(entry)

    def delete(self, entry_id: str, expected_revision: int) -> None:
        with self.database.session() as session:
            entry = session.get(PronunciationEntry, entry_id)
            if entry is None:
                raise KeyError(entry_id)
            if entry.revision != expected_revision:
                raise ValueError("The pronunciation changed in another client.")
            session.delete(entry)

    def resolve(
        self,
        text: str,
        *,
        session_id: str | None,
        language: str,
        backend: str = "*",
    ) -> list[dict[str, Any]]:
        """Resolve reviewed entries with session overrides winning over global ones."""
        normalized_language = normalize_language(language)
        normalized_backend = normalize_backend(backend)
        with self.database.session() as session:
            scope_filter = PronunciationEntry.session_id.is_(None)
            if session_id:
                scope_filter = or_(
                    PronunciationEntry.session_id.is_(None),
                    PronunciationEntry.session_id == session_id,
                )
            entries = list(
                session.scalars(
                    select(PronunciationEntry).where(
                        PronunciationEntry.status == "reviewed",
                        scope_filter,
                        PronunciationEntry.language.in_(
                            tuple(dict.fromkeys((normalized_language, "und")))
                        ),
                        PronunciationEntry.backend.in_(
                            tuple(dict.fromkeys((normalized_backend, "*")))
                        ),
                    )
                ).all()
            )

        ranked = sorted(
            entries,
            key=lambda item: (
                item.session_id == session_id and bool(session_id),
                item.language == normalized_language,
                item.backend == normalized_backend,
                len(item.source_form),
                item.updated_at,
            ),
            reverse=True,
        )
        # Pick the highest-precedence entry for each normalized written form
        # first.  A session entry therefore overrides its global counterpart,
        # while genuinely different forms still participate in longest-match
        # selection below.
        chosen: dict[str, PronunciationEntry] = {}
        for entry in ranked:
            if entry.normalized_form in chosen:
                continue
            if _bounded_contains(text, entry.source_form):
                chosen[entry.normalized_form] = entry
        return [
            self.payload(item)
            for item in sorted(
                chosen.values(),
                key=lambda item: (-len(item.normalized_form), item.normalized_form),
            )
        ]

    def propose(
        self,
        *,
        session_id: str,
        source_form: str,
        phonetic: str,
        language: str,
        backend: str = "*",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = self._normalized_values(
            {
                "scope": "session",
                "session_id": session_id,
                "source_form": source_form,
                "language": language,
                "phonetic": phonetic,
                "backend": backend,
                "status": "proposed",
                "source": "speech_plan",
                "metadata": metadata or {},
            }
        )
        with self.database.session() as session:
            existing = self._duplicate(session, values)
            if existing is not None:
                return self.payload(existing)
            entry = PronunciationEntry(**values)
            session.add(entry)
            session.flush()
            return self.payload(entry)
