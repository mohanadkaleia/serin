"""The ``push`` and ``pull`` sync engines (ENG-70 §3/§4).

``push`` drains the outbox to the server in ordered batches; ``pull`` mirrors
every readable stream from sequence 1 into the synced log, verbatim, advancing a
per-stream cursor in lockstep with fsynced pages.

The two together realize the two-store model: locally-authored events go up via
``push`` (POST /v1/events/batch), come back down via ``pull`` as the server's
authoritative copy, and land in ``streams/<id>/*.ndjson`` — which therefore holds
**only** server-served envelopes and stays byte-equal across clients and green
under ``verify``/``project``/``rebuild``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, Any, Final

from msgd.core import ids

from msgctl.append import _scan_stream, flock_exclusive
from msgctl.client import MsgClient, ProtocolError
from msgctl.credentials import META_STREAM_NAME, read_cursors, write_cursors
from msgctl.outbox import OutboxItem, read_all, remove
from msgctl.workspace import STREAM_LOCK, StreamInfo, Workspace, _fsync_dir, now_rfc3339

__all__ = ["PushResult", "PullResult", "push", "pull"]

#: Batch caps mirroring the server (``events_upload``): ≤100 events per batch and
#: a whole-request body well under the 1 MB cap (margin for the ``{"events":[…]}``
#: wrapper + separators).
_MAX_BATCH_ITEMS: Final = 100
_MAX_BATCH_BYTES: Final = 1024 * 1024 - 8192
#: Per-item over-estimate covering the array separator + ``httpx``'s spaced-
#: separator re-serialization (compact ``item.line`` underestimates the wire size).
_ITEM_SPACING_PAD: Final = 16

#: Pull page size — the server clamps ``limit`` into ``[1, 500]``; ask for the max.
_PULL_LIMIT: Final = 500


@dataclass(frozen=True)
class RejectedItem:
    """One permanently rejected outbox item (reported, then drained)."""

    event_id: str
    code: str
    detail: str


@dataclass
class PushResult:
    """Outcome of a :func:`push`: accepted count + any permanent rejections."""

    accepted: int = 0
    rejected: list[RejectedItem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


@dataclass
class PullResult:
    """Outcome of a :func:`pull`: streams seen, events written, streams newly registered."""

    streams: int = 0
    events: int = 0
    registered: list[str] = field(default_factory=list)


# --- push -------------------------------------------------------------------


def _batches(items: list[OutboxItem]) -> Iterator[list[OutboxItem]]:
    """Yield FIFO batches of ≤100 items and ≤~1 MB.

    ``item.line`` is the COMPACT ``{body, event_hash}`` serialization, whereas the
    request ``httpx`` builds re-serializes with spaced separators (``", "`` /
    ``": "``), so the real body is slightly larger than the sum measured here. The
    ``_ITEM_SPACING_PAD`` per item (a generous upper bound on the spacing overhead)
    plus the ``_MAX_BATCH_BYTES`` head-room below the hard 1 MB cap absorb that
    difference, so a batch that passes here never trips the server's 413.
    """
    batch: list[OutboxItem] = []
    size = 0
    for item in items:
        item_bytes = len(item.line.encode("utf-8")) + _ITEM_SPACING_PAD
        if batch and (len(batch) >= _MAX_BATCH_ITEMS or size + item_bytes > _MAX_BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += item_bytes
    if batch:
        yield batch


def push(ws: Workspace, client: MsgClient) -> PushResult:
    """Drain the outbox to the server in ordered batches (idempotent retry).

    Each batch is POSTed via :meth:`MsgClient.post_batch`, whose retry loop re-
    sends the SAME ``event_id``s on a transient fault so server idempotency
    (``UNIQUE(workspace_id, event_id)``) yields the original record — no
    duplicate. Drained ``event_id``s (both accepted — which return via ``pull`` —
    and permanently-rejected — which the client must stop retrying) are collected
    across all batches and removed from the outbox **once at the end**, avoiding a
    per-batch read+rewrite. Crash-safe: a crash before the final drain leaves the
    already-processed items queued, and the next push re-accepts them idempotently
    (server dedupe) then drains — no duplicate. A rejection makes
    :attr:`PushResult.ok` false so the CLI exits nonzero.
    """
    result = PushResult()
    drain: set[str] = set()
    for batch in _batches(read_all(ws)):
        resp = client.post_batch([{"body": it.body, "event_hash": it.event_hash} for it in batch])
        accepted = resp.get("accepted", [])
        rejected = resp.get("rejected", [])
        result.accepted += len(accepted)
        for rej in rejected:
            result.rejected.append(
                RejectedItem(
                    event_id=str(rej.get("event_id", "")),
                    code=str(rej.get("code", "")),
                    detail=str(rej.get("detail", "")),
                )
            )
        drain |= {str(a["event_id"]) for a in accepted}
        drain |= {str(r.get("event_id", "")) for r in rejected}
    remove(ws, drain)
    return result


# --- pull -------------------------------------------------------------------


def _safe_stream_id(value: Any) -> str:
    """Validate a server-supplied ``stream_id`` BEFORE it is used as a path component.

    SECURITY (path-traversal guard): every ``stream_id`` from a ``GET /v1/sync`` /
    ``GET /v1/events`` response flows into ``ws.stream_dir(sid) = streams_dir /
    sid`` (``workspace.py`` does zero sanitization) → ``mkdir(parents=True)`` +
    append of server-controlled bytes. A hostile/compromised server returning
    ``"../../../../tmp/evil"`` (or an absolute path) would otherwise write an
    attacker-controlled log OUTSIDE the workspace root, and ``verify`` — which runs
    its hash check only AFTER the write — cannot catch a path escape. So each id is
    checked against the repo's ``s_``-typed-ULID shape at the trust boundary and a
    bad one aborts the whole pull. The raw value is NEVER echoed (it could itself
    be a traversal payload); only its shape is reported.
    """
    if isinstance(value, str) and ids.is_valid_typed_id(value, ids.IdKind.STREAM):
        return value
    kind = type(value).__name__ if not isinstance(value, str) else f"str[len={len(value)}]"
    raise ProtocolError(
        f"server returned a stream_id that is not a valid 's_' typed ULID ({kind}); "
        "refusing to use it as a path component"
    )


def _register_streams(ws: Workspace, streams: list[dict[str, Any]]) -> list[str]:
    """Register every synced stream in ``workspace.json`` if absent (§4.6).

    A pulled stream dir with events but no manifest entry fails ``verify``
    (``unregistered_stream_dir``), so registration must precede writing any page.
    ``workspace-meta`` gets the reserved name (its server ``name`` may be null and
    the manifest's unique-name index needs a non-null name); a channel with a null
    name (private) falls back to its stream id. Runs under the workspace lock and
    re-reads the manifest fresh, matching ``resolve_or_create_stream``.
    """
    registered: list[str] = []
    with flock_exclusive(ws.lock_path):
        fresh = Workspace.open(ws.root)
        for s in streams:
            sid = _safe_stream_id(s["stream_id"])  # never register an unvalidated id
            if sid in fresh.streams:
                continue
            kind = str(s.get("kind", "channel"))
            if kind == "workspace-meta":
                name = META_STREAM_NAME
            else:
                name = s.get("name") or sid
            fresh.streams[sid] = StreamInfo(
                stream_id=sid, name=str(name), kind=kind, created_at=now_rfc3339()
            )
            registered.append(sid)
        if registered:
            fresh.write_manifest()
        ws.streams = fresh.streams
    return registered


def _resume_seq(ws: Workspace, stream_id: str) -> int:
    """The log-derived resume point: the max ``server_sequence`` durably on disk.

    **This is the crash-safety hinge (scan-on-open, matching ``append.py``).** The
    sidecar cursor is persisted only *after* a page is fsynced, so a crash in that
    window leaves durable page bytes with a STALE cursor. If the resume point came
    from the sidecar, ``get_events(after=stale)`` would re-return those durable
    events and append them twice (two lines at one ``server_sequence`` →
    ``verify`` fails, byte-equality breaks). Deriving it from the log instead makes
    the sidecar a pure optimization: the durable log is the source of truth.

    ``_scan_stream`` (reused verbatim from ``append.py``) repairs a torn trailing
    line and returns the max contiguous sequence; a stream with no dir yet → 0.
    Held under the per-stream ``flock``.
    """
    stream_dir = ws.stream_dir(stream_id)
    if not stream_dir.is_dir():
        return 0
    with flock_exclusive(stream_dir / STREAM_LOCK):
        return _scan_stream(stream_dir).last_seq


def _write_page(ws: Workspace, stream_id: str, events: list[dict[str, Any]]) -> int:
    """Append a page verbatim to the stream's month files; return the new cursor.

    Each event is written with the **same** compact serialization the M0 log
    writer uses (``json.dumps(evt, ensure_ascii=False, separators=(",",":"))`` +
    ``"\\n"``) into ``<server_received_at[:7]>.ndjson`` — so both clients derive
    the same month split from the same server-supplied timestamp and their logs
    are byte-identical. All touched files are fsynced before returning; the caller
    advances + persists the cursor only after. Held under the per-stream ``flock``.

    Torn-line repair is not needed here: the pull loop derives its start from
    :func:`_resume_seq` (which repairs on open), and every page is fully fsynced
    before the next, so no torn trailing line can exist when this appends.
    """
    stream_dir = ws.stream_dir(stream_id)
    created_dir = not stream_dir.exists()
    stream_dir.mkdir(parents=True, exist_ok=True)
    if created_dir:
        _fsync_dir(ws.streams_dir)

    with flock_exclusive(stream_dir / STREAM_LOCK):
        open_files: dict[str, IO[bytes]] = {}
        new_files: set[str] = set()
        try:
            for evt in events:
                month = str(evt["server"]["server_received_at"])[:7]
                path = stream_dir / f"{month}.ndjson"
                key = str(path)
                if key not in open_files:
                    if not path.exists():
                        new_files.add(key)
                    open_files[key] = open(path, "ab")
                line = json.dumps(evt, ensure_ascii=False, separators=(",", ":")) + "\n"
                open_files[key].write(line.encode("utf-8"))
            for fh in open_files.values():
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            for fh in open_files.values():
                fh.close()
        if new_files:
            _fsync_dir(stream_dir)

    return int(events[-1]["server"]["server_sequence"])


def pull(ws: Workspace, client: MsgClient) -> PullResult:
    """Mirror every readable stream from sequence 1 into the synced log (§4).

    ``GET /v1/sync`` lists the readable streams; each is registered, then paged
    forward via ``GET /v1/events?after=<cursor>``, appended verbatim, with the
    cursor advanced + durably persisted **after** each page's bytes are fsynced.

    The per-stream resume point is ``max(sidecar_cursor, log_head)`` — the log is
    authoritative (:func:`_resume_seq`), so a crash between a page's fsync and its
    cursor-persist can never cause a re-append: on resume ``after=log_head`` skips
    every already-durable event. A fresh stream starts at 0 ≡ from seq 1.

    SECURITY: every server-supplied ``stream_id`` is validated with
    :func:`_safe_stream_id` **before** it is used as a filesystem path component or
    registered, so a hostile server cannot drive path traversal. (The only id that
    reaches a path is this validated top-level sync id; an event body's
    ``stream_id`` is written verbatim as hash-covered log data and never touches a
    path.)
    """
    result = PullResult()
    sync = client.get_sync()
    raw_streams = sync.get("streams", [])
    # Validate ALL ids up front — abort the whole pull before any path use/register.
    for s in raw_streams:
        _safe_stream_id(s.get("stream_id") if isinstance(s, dict) else None)
    streams = sorted(raw_streams, key=lambda s: str(s["stream_id"]))
    result.streams = len(streams)
    result.registered = _register_streams(ws, streams)

    cursors = read_cursors(ws)
    for s in streams:
        sid = _safe_stream_id(s["stream_id"])  # defense-in-depth at the path site
        # Log-derived resume (crash-safe): never trust the sidecar cursor alone.
        cursor = max(cursors.get(sid, 0), _resume_seq(ws, sid))
        if cursors.get(sid, 0) != cursor:
            # Reconcile a stale sidecar (e.g. crash before its persist) to the
            # durable log head so it stays a truthful optimization.
            cursors[sid] = cursor
            write_cursors(ws, cursors)
        while True:
            page = client.get_events(stream_id=sid, after=cursor, limit=_PULL_LIMIT)
            events = page.get("events", [])
            if not events:
                break
            cursor = _write_page(ws, sid, events)
            cursors[sid] = cursor
            write_cursors(ws, cursors)  # durable, only after the page is fsynced
            result.events += len(events)
            if not page.get("has_more"):
                break
    return result
