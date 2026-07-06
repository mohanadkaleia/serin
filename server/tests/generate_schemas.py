"""Deterministic generator for the published JSON Schemas (ENG-62).

Run it to (re)produce the committed schemas under ``docs/schemas/``::

    uv run python server/tests/generate_schemas.py

The M2 web client and M5 plugin authors consume these schemas (§2.2). They are
**generated from the Pydantic models** — :class:`~msgd.core.envelope.Envelope`
and every payload model registered in
:data:`~msgd.core.payloads.PAYLOAD_MODELS` (``message.created`` plus the M1 meta
types) — via ``model_json_schema()``, so the runtime models stay the single
source of truth and a hand-written schema can never silently drift from them. Each schema is
wrapped with a JSON Schema 2020-12 ``$schema``, a stable ``$id`` and a ``title``,
then written with a fixed deterministic serialization (``sort_keys=True``,
``indent=2``, ``ensure_ascii=True``, LF, trailing newline).

``server/tests/test_schemas.py`` regenerates each document in memory and asserts
byte-equality to the committed files. This is the same freeze discipline as
``vectors.json`` / ``generate_vectors.py``: the committed artifact is a reviewed,
diffable output, and any change fails the freeze test until the file is
regenerated.

``model_json_schema()`` output is pinned to the locked pydantic version
(``uv.lock``). A pydantic upgrade that changes schema output is therefore a
**deliberate regenerate-and-review event**, exactly like a vector change — run
this script, review the diff, and commit the regenerated files.

This file must stay importless of anything beyond ``msgd.core`` + stdlib so it
can run anywhere the package is installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from msgd.core.envelope import Envelope
from msgd.core.payloads import PAYLOAD_MODELS

#: Output location: <repo>/docs/schemas/ (this file lives in <repo>/server/tests/,
#: so parents[2] is the repo root). Created if absent.
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "docs" / "schemas"

_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_ID_BASE = "https://msg.dev/schemas"


def _wrap(model: type[Any], filename: str, title: str) -> dict[str, Any]:
    """Return ``model``'s JSON Schema wrapped with ``$schema``/``$id``/``title``."""
    doc: dict[str, Any] = dict(model.model_json_schema())
    # Our wrapper keys win over pydantic's own (e.g. its "title": "Envelope").
    doc["$schema"] = _SCHEMA_DIALECT
    doc["$id"] = f"{_ID_BASE}/{filename}"
    doc["title"] = title
    return doc


def build_documents() -> dict[str, dict[str, Any]]:
    """Map each committed filename to its wrapped schema document.

    The envelope is published on its own; every typed payload is published from
    the :data:`~msgd.core.payloads.PAYLOAD_MODELS` registry — one wrapped schema
    per ``(type, type_version)`` (ENG-73, ENG-65's M1-exit flag), so the M1 meta
    types (``workspace.created``, ``user.joined/left``, ``channel.*``,
    ``dm.created``, …) publish alongside ``message.created`` and can never drift
    from the models. Cross-language payload *test vectors* for the meta types are
    M2-deferred (they land with the TypeScript client that consumes them).
    """
    documents: dict[str, dict[str, Any]] = {
        "envelope.schema.json": _wrap(
            Envelope,
            "envelope.schema.json",
            "msg event envelope",
        ),
    }
    for (type_name, type_version), model in sorted(PAYLOAD_MODELS.items()):
        filename = f"{type_name}.v{type_version}.schema.json"
        documents[filename] = _wrap(
            model,
            filename,
            f"msg {type_name} payload (v{type_version})",
        )
    return documents


def serialize(document: dict[str, Any]) -> str:
    """Deterministic serialization: ASCII, sorted keys, indent=2, LF, trailing newline."""
    return json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def main() -> None:
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, document in build_documents().items():
        text = serialize(document)
        (SCHEMAS_DIR / filename).write_text(text, encoding="ascii", newline="\n")
        print(f"wrote {SCHEMAS_DIR / filename} ({len(text)} bytes)")


if __name__ == "__main__":
    main()
