"""Shared event library for msg, consumed by both the server (``msgd``) and the CLI (``msgctl``).

This is the single home for the protocol primitives that cross the server/CLI boundary.
The following modules land here in later M0 tickets:

- ``envelope``  — event envelope + payload models (ENG-54)
- ``jcs``       — RFC 8785 JSON Canonicalization Scheme (ENG-55)
- ``hashing``   — ``event_hash`` = SHA-256 over JCS of ``body`` (ENG-56)
- ``schemas``   — per-type payload schemas (ENG-54)
- ``testdata/vectors.json`` — shared canonicalization/hashing test vectors (ENG-55/56)

Constraint: ``msgd.core`` must stay free of server-only dependencies (FastAPI, SQLAlchemy,
asyncpg, ...) so the CLI can import it cheaply. Keep this subpackage import-light.
"""
