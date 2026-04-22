"""Unit tests for the chunked log-response assembler (Stage 3c of #345)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from cms.services.log_chunk_assembler import (
    ChunkProtocolError,
    ChunkStateError,
    FLAG_FINAL,
    LogChunkAssembler,
    MAGIC,
    VERSION,
    encode_frame,
    is_chunk_frame,
    parse_frame,
)


# ── Wire-format tests ────────────────────────────────────────────────


def test_is_chunk_frame_detects_magic():
    assert is_chunk_frame(encode_frame("r1", 0, 1, b"payload", is_final=True))
    assert not is_chunk_frame(b"")
    assert not is_chunk_frame(b"{\"type\":\"status\"}")
    assert not is_chunk_frame(b"LGC")  # too short
    assert not is_chunk_frame(b"ABCD" + b"rest")


def test_parse_frame_roundtrip():
    payload = b"\x00" * 500
    raw = encode_frame("req-abc", seq=3, total=5, payload=payload, is_final=False)
    parsed = parse_frame(raw)
    assert parsed.request_id == "req-abc"
    assert parsed.seq == 3
    assert parsed.total == 5
    assert parsed.is_final is False
    assert parsed.payload == payload


def test_parse_frame_final_flag():
    raw = encode_frame("r", 0, 1, b"last", is_final=True)
    parsed = parse_frame(raw)
    assert parsed.is_final is True


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"XYZ!",
        b"LGCK\x02\x00\x00",  # wrong version
        b"LGCK\x01\xff\xff",  # claims a 65535-byte request_id, truncated
    ],
)
def test_parse_frame_rejects_garbage(bad):
    with pytest.raises(ChunkProtocolError):
        parse_frame(bad)


def test_parse_frame_rejects_bad_total():
    # total = 0 is invalid
    import struct
    raw = b"LGCK\x01" + struct.pack("<H", 1) + b"r" + struct.pack("<HHB", 0, 0, 0)
    with pytest.raises(ChunkProtocolError):
        parse_frame(raw)


def test_parse_frame_rejects_seq_out_of_range():
    import struct
    raw = b"LGCK\x01" + struct.pack("<H", 1) + b"r" + struct.pack("<HHB", 5, 3, 0)
    with pytest.raises(ChunkProtocolError):
        parse_frame(raw)


def test_encode_frame_validations():
    with pytest.raises(ValueError):
        encode_frame("r", 5, 3, b"", is_final=False)
    with pytest.raises(ValueError):
        encode_frame("r", 0, 0, b"", is_final=False)
    with pytest.raises(ValueError):
        encode_frame("r" * 70000, 0, 1, b"", is_final=True)


# ── Assembler tests ──────────────────────────────────────────────────


def _assembler(**overrides) -> LogChunkAssembler:
    kwargs = dict(max_count=30, max_bytes=22_020_096, buffer_ttl_sec=300)
    kwargs.update(overrides)
    return LogChunkAssembler(**kwargs)


@pytest.mark.asyncio
async def test_happy_path_assembles_three_chunks():
    asm = _assembler()
    chunks = [b"aaa", b"bbb", b"ccc"]
    frames = [
        parse_frame(encode_frame("rid", i, 3, c, is_final=(i == 2)))
        for i, c in enumerate(chunks)
    ]

    result = await asm.ingest("dev", frames[0])
    assert result is None
    result = await asm.ingest("dev", frames[1])
    assert result is None
    result = await asm.ingest("dev", frames[2])
    assert result is not None
    assert result.device_id == "dev"
    assert result.request_id == "rid"
    assert result.data == b"aaabbbccc"
    # Buffer dropped after assembly.
    assert asm.pending_count() == 0


@pytest.mark.asyncio
async def test_out_of_order_assembles_correctly():
    asm = _assembler()
    # Middle and first frames arrive swapped; final frame (highest seq)
    # arrives last — which matches real-world single-WS ordering but
    # still exercises the "fill by seq index, not arrival order" code.
    f0 = parse_frame(encode_frame("rid", 0, 3, b"111", is_final=False))
    f1 = parse_frame(encode_frame("rid", 1, 3, b"222", is_final=False))
    f2 = parse_frame(encode_frame("rid", 2, 3, b"333", is_final=True))

    assert await asm.ingest("dev", f1) is None
    assert await asm.ingest("dev", f0) is None
    result = await asm.ingest("dev", f2)
    assert result is not None
    assert result.data == b"111222333"


@pytest.mark.asyncio
async def test_duplicate_seq_ignored():
    asm = _assembler()
    f0a = parse_frame(encode_frame("rid", 0, 2, b"first", is_final=False))
    f0b = parse_frame(encode_frame("rid", 0, 2, b"REDO!", is_final=False))
    f1 = parse_frame(encode_frame("rid", 1, 2, b"tail", is_final=True))

    assert await asm.ingest("dev", f0a) is None
    # Duplicate ignored — first copy wins.
    assert await asm.ingest("dev", f0b) is None
    result = await asm.ingest("dev", f1)
    assert result is not None
    assert result.data == b"firsttail"


@pytest.mark.asyncio
async def test_missing_seq_on_final_fails():
    asm = _assembler()
    f0 = parse_frame(encode_frame("rid", 0, 3, b"a", is_final=False))
    f2 = parse_frame(encode_frame("rid", 2, 3, b"c", is_final=True))

    assert await asm.ingest("dev", f0) is None
    with pytest.raises(ChunkStateError, match="chunks_missing"):
        await asm.ingest("dev", f2)
    # Buffer dropped after state error.
    assert asm.pending_count() == 0


@pytest.mark.asyncio
async def test_count_cap_rejected():
    asm = _assembler(max_count=5)
    over = parse_frame(encode_frame("rid", 0, 6, b"x", is_final=False))
    with pytest.raises(ChunkStateError, match="chunks_exceeded"):
        await asm.ingest("dev", over)
    assert asm.pending_count() == 0


@pytest.mark.asyncio
async def test_bytes_cap_rejected():
    asm = _assembler(max_bytes=1000)
    f0 = parse_frame(encode_frame("rid", 0, 2, b"x" * 600, is_final=False))
    f1 = parse_frame(encode_frame("rid", 1, 2, b"x" * 600, is_final=True))
    assert await asm.ingest("dev", f0) is None
    with pytest.raises(ChunkStateError, match="bytes_exceeded"):
        await asm.ingest("dev", f1)
    assert asm.pending_count() == 0


@pytest.mark.asyncio
async def test_total_mismatch_rejected():
    asm = _assembler()
    f0 = parse_frame(encode_frame("rid", 0, 3, b"a", is_final=False))
    f1_bad = parse_frame(encode_frame("rid", 1, 5, b"b", is_final=False))
    assert await asm.ingest("dev", f0) is None
    with pytest.raises(ChunkStateError, match="total_changed"):
        await asm.ingest("dev", f1_bad)
    assert asm.pending_count() == 0


@pytest.mark.asyncio
async def test_different_requests_isolated():
    asm = _assembler()
    rA = parse_frame(encode_frame("ridA", 0, 1, b"A", is_final=True))
    rB = parse_frame(encode_frame("ridB", 0, 1, b"B", is_final=True))
    resA = await asm.ingest("devA", rA)
    resB = await asm.ingest("devB", rB)
    assert resA.data == b"A"
    assert resB.data == b"B"


@pytest.mark.asyncio
async def test_same_request_id_different_devices_isolated():
    """Keyed by ``(device_id, request_id)`` — same request_id from two
    devices can coexist (request ids are UUIDs in practice, but the
    assembler shouldn't rely on global uniqueness)."""
    asm = _assembler()
    f_a = parse_frame(encode_frame("rid", 0, 1, b"hostA", is_final=True))
    f_b = parse_frame(encode_frame("rid", 0, 1, b"hostB", is_final=True))
    a = await asm.ingest("dev-a", f_a)
    b = await asm.ingest("dev-b", f_b)
    assert a.data == b"hostA"
    assert b.data == b"hostB"
    assert a.device_id == "dev-a"
    assert b.device_id == "dev-b"


# ── Reaper tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reap_expired_drops_stalled_and_returns_metadata():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = {"t": now}
    asm = LogChunkAssembler(
        max_count=10, max_bytes=10_000, buffer_ttl_sec=60,
        clock=lambda: clock["t"],
    )

    f0 = parse_frame(encode_frame("rid", 0, 2, b"first", is_final=False))
    await asm.ingest("dev", f0)
    assert asm.pending_count() == 1

    # Before TTL: nothing reaped.
    clock["t"] = now + timedelta(seconds=30)
    assert await asm.reap_expired() == []
    assert asm.pending_count() == 1

    # Past TTL: reaped.
    clock["t"] = now + timedelta(seconds=120)
    reaped = await asm.reap_expired()
    assert len(reaped) == 1
    assert reaped[0].device_id == "dev"
    assert reaped[0].request_id == "rid"
    assert "chunk_buffer_timeout" in reaped[0].reason
    assert asm.pending_count() == 0


@pytest.mark.asyncio
async def test_reap_preserves_active_buffers():
    """A buffer that received a frame recently (after another one
    stalled) should not be reaped."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = {"t": now}
    asm = LogChunkAssembler(
        max_count=10, max_bytes=10_000, buffer_ttl_sec=60,
        clock=lambda: clock["t"],
    )

    # Stalled buffer.
    await asm.ingest("dev", parse_frame(
        encode_frame("old", 0, 2, b"x", is_final=False),
    ))

    # Active buffer — added much later.
    clock["t"] = now + timedelta(seconds=90)
    await asm.ingest("dev", parse_frame(
        encode_frame("new", 0, 2, b"y", is_final=False),
    ))

    reaped = await asm.reap_expired()
    assert [r.request_id for r in reaped] == ["old"]
    assert asm.pending_count() == 1
    assert asm.has_buffer("dev", "new")


# ── drop() helper ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drop_removes_buffer():
    asm = _assembler()
    await asm.ingest(
        "dev", parse_frame(encode_frame("rid", 0, 2, b"x", is_final=False)),
    )
    assert asm.has_buffer("dev", "rid")
    assert await asm.drop("dev", "rid") is True
    assert asm.has_buffer("dev", "rid") is False
    assert await asm.drop("dev", "rid") is False


# ── Large-ish payload smoke ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_assembles_many_chunks_preserving_bytes():
    asm = _assembler(max_count=30, max_bytes=30 * 100_000)
    total = 20
    chunks = [bytes([i % 256]) * 1000 for i in range(total)]
    for i, c in enumerate(chunks):
        frame = parse_frame(
            encode_frame("big", i, total, c, is_final=(i == total - 1))
        )
        res = await asm.ingest("dev", frame)
        if i < total - 1:
            assert res is None
        else:
            assert res is not None
            assert res.data == b"".join(chunks)
