"""Chunked log-response assembler (Stage 3c of #345).

Pi firmware advertising the ``logs_chunk_v1`` capability splits log
bundles larger than the single-message WPS cap (~1 MiB) into a
sequence of binary WebSocket frames.  Each frame is prefixed with
the ``LGCK`` magic so the WS receive loop can route it apart from
text (JSON) messages.

This module owns the per-process reassembly buffer, concatenates
chunks in ``seq`` order on the ``is_final`` flag, writes the combined
tar.gz to blob storage, and flips the ``log_requests`` outbox row to
``ready``.  A background reaper expires stalled transfers so a Pi
that crashes mid-upload doesn't leak memory (and the matching outbox
row flips to ``failed`` for the user to see).

Multi-replica note: the buffer is process-local.  That's correct
under today's transport (``device_transport=local``) and under WPS
with sticky routing — all frames for a given device land on the
same replica as the REGISTER.  If a device reconnects mid-stream to
a different replica (rare), the orphaned buffer expires via the TTL
reaper, the outbox row flips to ``failed``, and the Pi's drainer-
driven retry re-requests cleanly.

Wire format for ``LGCK`` frames (all integers little-endian)::

    +----------+-------+-----------------+----------------+------+-------+-------+
    | "LGCK"   | ver   | request_id_len  | request_id     | seq  | total | flags |
    |  4 bytes | u8    |  u16            |  UTF-8 N bytes |  u16 |  u16  |  u8   |
    +----------+-------+-----------------+----------------+------+-------+-------+
    | chunk payload (rest of the frame, raw gzipped-tar bytes)                   |
    +----------------------------------------------------------------------------+

``flags`` bit 0 (``0x01``) indicates the final chunk in the sequence.
``seq`` is 0-based and must satisfy ``seq < total``.  ``total`` is
fixed for the lifetime of one ``request_id`` — a mismatch on a later
frame aborts the transfer.
"""

from __future__ import annotations

import asyncio
import io
import logging
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger("agora.cms.log_chunks")


# ── Wire format constants ────────────────────────────────────────────

MAGIC = b"LGCK"
VERSION = 1
_HEADER_PREFIX = struct.Struct("<4sB H")  # magic, version, request_id_len
_TRAILER = struct.Struct("<HHB")  # seq, total, flags
FLAG_FINAL = 0x01
# Magic(4) + version(1) + rid_len(2) + seq(2) + total(2) + flags(1).
# The request_id bytes live between rid_len and seq.
_FIXED_HEADER_BYTES = 4 + 1 + 2 + 2 + 2 + 1


# ── Errors ───────────────────────────────────────────────────────────

class ChunkProtocolError(Exception):
    """Frame violated the LGCK wire contract (bad magic, short, etc.)."""


class ChunkStateError(Exception):
    """Frame was well-formed but inconsistent with existing buffer state.

    Examples: ``total`` changed between frames; ``seq`` out of range;
    count/bytes cap exceeded; duplicate ``is_final`` with missing seqs.
    """


# ── Parsed frame ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class ChunkFrame:
    request_id: str
    seq: int
    total: int
    is_final: bool
    payload: bytes


def is_chunk_frame(data: bytes) -> bool:
    """Quick O(1) check — does ``data`` start with the LGCK magic?"""
    return len(data) >= 4 and data[:4] == MAGIC


def parse_frame(data: bytes) -> ChunkFrame:
    """Parse one ``LGCK`` binary frame.

    Raises :class:`ChunkProtocolError` on any wire-format violation.
    Does not enforce buffer-level invariants — that's the assembler's
    job (see :meth:`LogChunkAssembler.ingest`).
    """
    if len(data) < _FIXED_HEADER_BYTES:
        raise ChunkProtocolError(
            f"frame too short ({len(data)} < {_FIXED_HEADER_BYTES})"
        )
    magic, version, rid_len = _HEADER_PREFIX.unpack_from(data, 0)
    if magic != MAGIC:
        raise ChunkProtocolError(f"bad magic {magic!r}")
    if version != VERSION:
        raise ChunkProtocolError(f"unsupported version {version}")
    rid_start = _HEADER_PREFIX.size
    rid_end = rid_start + rid_len
    if rid_end + _TRAILER.size > len(data):
        raise ChunkProtocolError("truncated request_id or trailer")
    try:
        request_id = data[rid_start:rid_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChunkProtocolError(f"non-utf8 request_id: {exc}") from exc
    seq, total, flags = _TRAILER.unpack_from(data, rid_end)
    if total == 0:
        raise ChunkProtocolError("total must be >= 1")
    if seq >= total:
        raise ChunkProtocolError(f"seq {seq} out of range for total {total}")
    payload = data[rid_end + _TRAILER.size:]
    return ChunkFrame(
        request_id=request_id,
        seq=seq,
        total=total,
        is_final=bool(flags & FLAG_FINAL),
        payload=payload,
    )


def encode_frame(
    request_id: str,
    seq: int,
    total: int,
    payload: bytes,
    *,
    is_final: bool,
) -> bytes:
    """Produce a wire-format ``LGCK`` frame.  Used by tests; Pi firmware
    has its own encoder in ``agora/cms_client/service.py`` that must
    stay in lockstep with this one."""
    rid_bytes = request_id.encode("utf-8")
    if len(rid_bytes) > 0xFFFF:
        raise ValueError("request_id too long")
    if total < 1 or total > 0xFFFF:
        raise ValueError("total out of range")
    if seq < 0 or seq >= total:
        raise ValueError("seq out of range")
    header = _HEADER_PREFIX.pack(MAGIC, VERSION, len(rid_bytes)) + rid_bytes
    trailer = _TRAILER.pack(seq, total, FLAG_FINAL if is_final else 0)
    return header + trailer + payload


# ── Assembly buffer ──────────────────────────────────────────────────

@dataclass
class _Buffer:
    device_id: str
    request_id: str
    total: int
    created_at: datetime
    last_updated_at: datetime
    chunks: dict[int, bytes] = field(default_factory=dict)
    total_bytes: int = 0
    saw_final: bool = False

    def has_all(self) -> bool:
        return len(self.chunks) == self.total

    def assemble(self) -> bytes:
        parts = [self.chunks[i] for i in range(self.total)]
        return b"".join(parts)


@dataclass(frozen=True)
class AssembledBundle:
    device_id: str
    request_id: str
    data: bytes


@dataclass(frozen=True)
class ReapedTransfer:
    device_id: str
    request_id: str
    reason: str


class LogChunkAssembler:
    """Process-local buffer for partially-received chunked log uploads.

    Methods are synchronous — the caller drives I/O (blob write,
    outbox flip) after :meth:`ingest` returns an :class:`AssembledBundle`.

    Thread-safety: guarded by an internal :class:`asyncio.Lock` so
    concurrent frame arrivals from distinct tasks can't race.  In
    practice the WS receive loop is single-tasked per connection, so
    the lock is mostly belt-and-suspenders.
    """

    def __init__(
        self,
        *,
        max_count: int,
        max_bytes: int,
        buffer_ttl_sec: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_count < 1:
            raise ValueError("max_count must be >= 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        if buffer_ttl_sec <= 0:
            raise ValueError("buffer_ttl_sec must be > 0")
        self._max_count = int(max_count)
        self._max_bytes = int(max_bytes)
        self._ttl = timedelta(seconds=buffer_ttl_sec)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._buffers: dict[tuple[str, str], _Buffer] = {}
        self._lock = asyncio.Lock()

    # -- introspection ------------------------------------------------

    def pending_count(self) -> int:
        return len(self._buffers)

    def has_buffer(self, device_id: str, request_id: str) -> bool:
        return (device_id, request_id) in self._buffers

    # -- ingest -------------------------------------------------------

    async def ingest(
        self, device_id: str, frame: ChunkFrame,
    ) -> AssembledBundle | None:
        """Add ``frame`` to the buffer for ``(device_id, request_id)``.

        Returns an :class:`AssembledBundle` when ``is_final`` is set and
        every sequence number has arrived.  Returns ``None`` for
        intermediate frames or when waiting on a late ``is_final``.

        Raises :class:`ChunkStateError` on buffer-level violations
        (e.g. ``total`` exceeds cap, assembled size exceeds cap,
        ``is_final`` with missing seqs).  The caller is expected to
        catch and translate this into an outbox ``failed`` + drop the
        buffer via :meth:`drop`.
        """
        async with self._lock:
            key = (device_id, frame.request_id)
            now = self._clock()
            buf = self._buffers.get(key)
            if buf is None:
                if frame.total > self._max_count:
                    raise ChunkStateError(
                        f"chunks_exceeded: total {frame.total} > "
                        f"max_count {self._max_count}"
                    )
                buf = _Buffer(
                    device_id=device_id,
                    request_id=frame.request_id,
                    total=frame.total,
                    created_at=now,
                    last_updated_at=now,
                )
                self._buffers[key] = buf
            elif buf.total != frame.total:
                # Drop the buffer — the Pi's second pass can't recover.
                self._buffers.pop(key, None)
                raise ChunkStateError(
                    f"total_changed: saw {buf.total}, frame has {frame.total}"
                )

            buf.last_updated_at = now

            if frame.seq in buf.chunks:
                # Duplicate — ignore.  Don't error: WPS can redeliver on
                # transient network blips and the first copy is authoritative.
                logger.debug(
                    "log_chunks: duplicate seq %d for (%s, %s) — ignored",
                    frame.seq, device_id, frame.request_id,
                )
                if frame.is_final:
                    buf.saw_final = True
                if buf.saw_final and buf.has_all():
                    return self._finalise(key, buf)
                return None

            projected = buf.total_bytes + len(frame.payload)
            if projected > self._max_bytes:
                self._buffers.pop(key, None)
                raise ChunkStateError(
                    f"bytes_exceeded: assembled size would be {projected} "
                    f"bytes > max_bytes {self._max_bytes}"
                )

            buf.chunks[frame.seq] = frame.payload
            buf.total_bytes = projected

            if frame.is_final:
                buf.saw_final = True

            if buf.saw_final:
                if buf.has_all():
                    return self._finalise(key, buf)
                missing = sorted(set(range(buf.total)) - buf.chunks.keys())
                # Only fail when the final frame *is* the latest — if
                # ``is_final`` arrived out of order (seq < total-1) the
                # remaining chunks may still be in flight.  That said,
                # the Pi always sends the final chunk last in practice,
                # so a missing-seq state on ``saw_final`` is terminal.
                if frame.is_final:
                    self._buffers.pop(key, None)
                    raise ChunkStateError(
                        f"chunks_missing: final frame received but seqs "
                        f"{missing} still missing"
                    )
            return None

    def _finalise(self, key: tuple[str, str], buf: _Buffer) -> AssembledBundle:
        data = buf.assemble()
        self._buffers.pop(key, None)
        return AssembledBundle(
            device_id=buf.device_id,
            request_id=buf.request_id,
            data=data,
        )

    # -- explicit drop ------------------------------------------------

    async def drop(self, device_id: str, request_id: str) -> bool:
        """Drop the buffer for the given pair without assembly.

        Used when the caller decides the transfer is doomed (e.g. the
        outbox flip to ``failed`` already ran)."""
        async with self._lock:
            return self._buffers.pop((device_id, request_id), None) is not None

    # -- reaper -------------------------------------------------------

    async def reap_expired(self, now: datetime | None = None) -> list[ReapedTransfer]:
        """Remove buffers whose ``last_updated_at`` is older than the TTL.

        Returns the list of reaped ``(device_id, request_id)`` pairs
        so the caller can flip the matching outbox rows to ``failed``.
        """
        now = now or self._clock()
        cutoff = now - self._ttl
        expired: list[ReapedTransfer] = []
        async with self._lock:
            dead: list[tuple[str, str]] = []
            for key, buf in self._buffers.items():
                if buf.last_updated_at <= cutoff:
                    dead.append(key)
                    expired.append(
                        ReapedTransfer(
                            device_id=buf.device_id,
                            request_id=buf.request_id,
                            reason=(
                                "chunk_buffer_timeout: last frame received at "
                                f"{buf.last_updated_at.isoformat()} "
                                f"(TTL {self._ttl.total_seconds():.0f}s)"
                            ),
                        )
                    )
            for key in dead:
                self._buffers.pop(key, None)
        return expired


# ── Module-level singleton ───────────────────────────────────────────

_assembler: LogChunkAssembler | None = None


def init_assembler(settings: Any) -> LogChunkAssembler:
    """Initialise the process-wide assembler singleton from settings."""
    global _assembler
    _assembler = LogChunkAssembler(
        max_count=int(getattr(settings, "log_chunk_max_count", 30)),
        max_bytes=int(getattr(settings, "log_chunk_max_bytes", 22_020_096)),
        buffer_ttl_sec=float(getattr(settings, "log_chunk_buffer_ttl_sec", 300)),
    )
    logger.info(
        "Log chunk assembler initialised (max_count=%d, max_bytes=%d, ttl=%.0fs)",
        _assembler._max_count, _assembler._max_bytes, _assembler._ttl.total_seconds(),
    )
    return _assembler


def get_assembler() -> LogChunkAssembler:
    if _assembler is None:
        raise RuntimeError(
            "Log chunk assembler not initialised — call init_assembler() first"
        )
    return _assembler


def set_assembler(assembler: LogChunkAssembler | None) -> None:
    """Test helper — swap in a pre-built assembler."""
    global _assembler
    _assembler = assembler


# ── Integration: frame → blob + outbox ───────────────────────────────

async def handle_frame(
    db,
    *,
    device_id: str,
    frame_bytes: bytes,
    assembler: LogChunkAssembler | None = None,
) -> AssembledBundle | None:
    """Parse ``frame_bytes``, feed it to the assembler, and — on the
    final frame — write the reassembled tar.gz to blob storage and
    flip the outbox row to ``ready``.

    Returns the :class:`AssembledBundle` when assembly completed on
    this call, ``None`` otherwise (intermediate frame, malformed, or
    buffer-level failure).

    All failures are caught here so a bad frame can't drop the WS
    connection — worst case we log + mark the outbox row ``failed``
    and the Pi retries via the drainer.
    """
    from cms.services import log_outbox
    from cms.services.log_blob import write_log_blob

    asm = assembler or get_assembler()

    try:
        frame = parse_frame(frame_bytes)
    except ChunkProtocolError as exc:
        logger.warning(
            "log_chunks: protocol error from %s: %s", device_id, exc,
        )
        return None

    try:
        bundle = await asm.ingest(device_id, frame)
    except ChunkStateError as exc:
        logger.warning(
            "log_chunks: state error on %s request %s: %s",
            device_id, frame.request_id, exc,
        )
        try:
            await log_outbox.mark_failed(db, frame.request_id, error=str(exc))
            await db.commit()
        except Exception:
            logger.exception(
                "log_chunks: failed to mark outbox failed for %s/%s",
                device_id, frame.request_id,
            )
        return None

    if bundle is None:
        return None

    blob_path = f"{bundle.device_id}/{bundle.request_id}.tar.gz"
    try:
        await write_log_blob(blob_path, bundle.data)
        await log_outbox.mark_ready(
            db, bundle.request_id,
            blob_path=blob_path, size_bytes=len(bundle.data),
        )
        await db.commit()
    except Exception:
        logger.exception(
            "log_chunks: failed to persist assembled bundle for %s/%s",
            bundle.device_id, bundle.request_id,
        )
        try:
            await log_outbox.mark_failed(
                db, bundle.request_id, error="chunk_persist_failed",
            )
            await db.commit()
        except Exception:
            logger.exception(
                "log_chunks: failed to mark outbox failed after persist error",
            )
        return None

    logger.info(
        "log_chunks: assembled %d bytes for %s/%s (%d chunks)",
        len(bundle.data), bundle.device_id, bundle.request_id, frame.total,
    )
    return bundle


# ── Reaper loop ──────────────────────────────────────────────────────

async def run_reaper_loop(
    session_factory_getter: Callable[[], Any],
    *,
    assembler_getter: Callable[[], LogChunkAssembler] = get_assembler,
    settings: Any,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the TTL reaper forever until ``stop_event`` is set.

    Each tick pulls expired buffers out of the assembler and, for each
    one, flips the matching ``log_requests`` row to ``failed`` so the
    user can see the transfer timed out.  Rows are addressed by
    ``request_id`` alone — device_id is only used for logging.
    """
    from cms.services import log_outbox

    interval = float(getattr(settings, "log_chunk_reaper_interval_sec", 5.0))
    if stop_event is None:
        stop_event = asyncio.Event()
    logger.info("Log chunk reaper loop started (interval=%.1fs)", interval)
    try:
        while not stop_event.is_set():
            try:
                asm = assembler_getter()
                expired = await asm.reap_expired()
                if expired:
                    factory = session_factory_getter()
                    if factory is None:
                        logger.warning(
                            "log_chunk_reaper: %d expired buffers but "
                            "session factory not initialised",
                            len(expired),
                        )
                    else:
                        async with factory() as db:
                            try:
                                for item in expired:
                                    try:
                                        await log_outbox.mark_failed(
                                            db, item.request_id, error=item.reason,
                                        )
                                    except Exception:
                                        logger.exception(
                                            "log_chunk_reaper: mark_failed "
                                            "failed for %s/%s",
                                            item.device_id, item.request_id,
                                        )
                                await db.commit()
                            except BaseException:
                                await db.rollback()
                                raise
                    for item in expired:
                        logger.warning(
                            "log_chunks: reaped stalled transfer %s/%s (%s)",
                            item.device_id, item.request_id, item.reason,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("log_chunk_reaper: unexpected tick error")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("Log chunk reaper loop stopped")
