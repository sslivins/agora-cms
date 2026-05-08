"""Unit + integration tests for ``cms.services.imager``.

The pure-string tests (``parse_boot_partition_offset``) run anywhere.
The integration tests gate on the presence of ``parted``, ``mcopy`` and
``xz`` on PATH, so they execute in CI (Linux, with ``mtools parted
xz-utils`` installed) and skip on Windows dev boxes.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cms.services.imager import (
    FLEET_ENV_FILENAME,
    ImagerError,
    _Tools,
    boot_partition_offset,
    build_provisioned,
    inject_fleet_env,
    parse_boot_partition_offset,
    read_fleet_env,
)


# ── Pure parser tests (run on any platform) ──────────────────────────


PARTED_JSON_PI_LIKE = """{
  "disk": {
    "path": "/tmp/agora.img",
    "size": "536870912B",
    "model": "",
    "transport": "file",
    "logical-sector-size": 512,
    "physical-sector-size": 512,
    "partition-table": "msdos",
    "partitions": [
      {
        "number": 1,
        "start": "4194304B",
        "end": "541065215B",
        "size": "536870912B",
        "type": "primary",
        "filesystem": "fat32",
        "flags": ["lba"]
      },
      {
        "number": 2,
        "start": "541065216B",
        "end": "1073741823B",
        "size": "532676608B",
        "type": "primary",
        "filesystem": "ext4"
      }
    ]
  }
}
"""


def test_parse_offset_finds_first_fat():
    assert parse_boot_partition_offset(PARTED_JSON_PI_LIKE) == 4194304


def test_parse_offset_accepts_int_start():
    json_blob = """
    {"disk": {"partitions": [
        {"filesystem": "fat16", "start": 1048576}
    ]}}
    """
    assert parse_boot_partition_offset(json_blob) == 1048576


def test_parse_offset_case_insensitive_fs():
    json_blob = """
    {"disk": {"partitions": [
        {"filesystem": "FAT32", "start": "2048B"}
    ]}}
    """
    assert parse_boot_partition_offset(json_blob) == 2048


def test_parse_offset_skips_non_fat_first():
    json_blob = """
    {"disk": {"partitions": [
        {"filesystem": "ext4", "start": "1024B"},
        {"filesystem": "fat32", "start": "8192B"}
    ]}}
    """
    assert parse_boot_partition_offset(json_blob) == 8192


def test_parse_offset_no_fat_partition_raises():
    json_blob = """
    {"disk": {"partitions": [
        {"filesystem": "ext4", "start": "1024B"}
    ]}}
    """
    with pytest.raises(ImagerError, match="no FAT partition"):
        parse_boot_partition_offset(json_blob)


def test_parse_offset_no_partitions_raises():
    json_blob = '{"disk": {"partitions": []}}'
    with pytest.raises(ImagerError, match="no partitions"):
        parse_boot_partition_offset(json_blob)


def test_parse_offset_invalid_json_raises():
    with pytest.raises(ImagerError, match="parted JSON"):
        parse_boot_partition_offset("not json {")


def test_parse_offset_missing_disk_raises():
    with pytest.raises(ImagerError, match="missing 'disk'"):
        parse_boot_partition_offset('{"foo": "bar"}')


def test_parse_offset_fat_without_start_raises():
    json_blob = """
    {"disk": {"partitions": [
        {"filesystem": "fat32"}
    ]}}
    """
    with pytest.raises(ImagerError, match="missing 'start' offset"):
        parse_boot_partition_offset(json_blob)


# ── Pure-function guard tests (no tools needed) ──────────────────────


def test_inject_fleet_env_rejects_nul_bytes(tmp_path: Path):
    # Guard fires before the tool discovery call, so no tools needed.
    img = tmp_path / "fake.img"
    img.write_bytes(b"x")
    with pytest.raises(ImagerError, match="NUL byte"):
        inject_fleet_env(img, "key=value\x00bad\n")


def test_inject_fleet_env_rejects_oversized(tmp_path: Path):
    img = tmp_path / "fake.img"
    img.write_bytes(b"x")
    huge = "k=v\n" * (20_000)  # > 64 KiB
    with pytest.raises(ImagerError, match="too large"):
        inject_fleet_env(img, huge)


@pytest.mark.parametrize(
    "bad_name",
    [
        "../escape.img.xz",
        "sub/dir.img.xz",
        "name.img",
        "name.xz",
        "name with spaces.img.xz",
        "",
    ],
)
def test_build_provisioned_rejects_bad_output_name(tmp_path: Path, bad_name):
    base = tmp_path / "fake.img.xz"
    base.write_bytes(b"x")
    with pytest.raises(ImagerError, match="invalid output_name"):
        build_provisioned(base, "x=y\n", tmp_path / "scratch", output_name=bad_name)


# ── Integration tests (require parted + mcopy + xz) ──────────────────


def _have_tools() -> bool:
    return all(
        shutil.which(tool) is not None
        for tool in ("parted", "mcopy", "mformat", "xz")
    )


needs_imager_tools = pytest.mark.skipif(
    not _have_tools(),
    reason="requires parted, mcopy + mformat (mtools), and xz on PATH",
)


def _make_partitioned_fat_image(path: Path) -> Path:
    """Create a 64 MiB image with a single FAT32 partition.

    The size matches typical Pi boot partitions and gives mformat
    enough clusters for FAT32 across distro versions.
    """
    total_bytes = 64 * 1024 * 1024
    with path.open("wb") as fh:
        fh.truncate(total_bytes)

    subprocess.run(
        ["parted", "-s", str(path), "mklabel", "msdos"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "parted",
            "-s",
            str(path),
            "mkpart",
            "primary",
            "fat32",
            "1MiB",
            "100%",
        ],
        check=True,
        capture_output=True,
    )

    offset = 1024 * 1024
    subprocess.run(
        [
            "mformat",
            "-i",
            f"{path}@@{offset}",
            "-v",
            "AGORA",
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def partitioned_fat_image(tmp_path: Path) -> Path:
    if not _have_tools():
        pytest.skip("requires parted + mtools")
    return _make_partitioned_fat_image(tmp_path / "test.img")


@needs_imager_tools
def test_boot_partition_offset_real_image(partitioned_fat_image: Path):
    offset = boot_partition_offset(partitioned_fat_image)
    # parted aligns at 1 MiB boundary
    assert offset == 1024 * 1024


@needs_imager_tools
def test_inject_fleet_env_writes_file(partitioned_fat_image: Path):
    payload = "AGORA_FLEET_ID=test-fleet\nAGORA_FLEET_SECRET=abc123\n"
    inject_fleet_env(partitioned_fat_image, payload)

    readback = read_fleet_env(partitioned_fat_image)
    assert readback == payload


@needs_imager_tools
def test_inject_fleet_env_overwrites(partitioned_fat_image: Path):
    inject_fleet_env(partitioned_fat_image, "first=value\n")
    inject_fleet_env(partitioned_fat_image, "second=value\n")
    assert read_fleet_env(partitioned_fat_image) == "second=value\n"


@needs_imager_tools
def test_inject_fleet_env_with_explicit_offset(partitioned_fat_image: Path):
    # Compute once, pass in — verifies the optimization path works.
    tools = _Tools.discover()
    offset = boot_partition_offset(partitioned_fat_image, tools=tools)
    inject_fleet_env(
        partitioned_fat_image,
        "explicit=ok\n",
        boot_offset=offset,
        tools=tools,
    )
    assert read_fleet_env(partitioned_fat_image, boot_offset=offset, tools=tools) == "explicit=ok\n"


@needs_imager_tools
def test_build_provisioned_end_to_end(tmp_path: Path):
    base_img = _make_partitioned_fat_image(tmp_path / "agora-base-v1.img")
    base_xz = tmp_path / "agora-base-v1.img.xz"
    subprocess.run(
        ["xz", "-z", "-1", "--keep", str(base_img)],
        check=True,
        capture_output=True,
    )
    assert base_xz.exists()

    scratch = tmp_path / "scratch"
    fleet_env = "AGORA_FLEET_ID=prod-east\nAGORA_FLEET_SECRET=xyz\n"

    out = build_provisioned(
        base_xz,
        fleet_env,
        scratch,
        output_name="prod-east-v1.img.xz",
    )

    assert out == scratch / "prod-east-v1.img.xz"
    assert out.is_file()
    assert out.stat().st_size > 0

    # Decompress + verify the env file is present
    out_img = tmp_path / "verify.img"
    with out_img.open("wb") as fh:
        subprocess.run(
            ["xz", "-d", "-c", str(out)],
            stdout=fh,
            check=True,
        )
    contents = read_fleet_env(out_img)
    assert contents == fleet_env


@needs_imager_tools
def test_build_provisioned_emits_progress(tmp_path: Path):
    """Watcher threads emit monotonic, in-bounds progress for both xz stages.

    Pins the wire contract that ``worker.imager_handlers`` and the
    ``imager.html`` UI rely on:

    * Both ``decompressing`` and ``compressing`` stages fire at least
      once -- the imager-progress revamp explicitly exists to
      eliminate the multi-minute black-hole at 35%.
    * Pct values are non-decreasing within each stage and never spill
      past their stage's end constant before the next stage starts.
    """
    base_img = _make_partitioned_fat_image(tmp_path / "agora-base-v1.img")
    subprocess.run(
        ["xz", "-z", "-1", "--keep", str(base_img)],
        check=True,
        capture_output=True,
    )
    base_xz = tmp_path / "agora-base-v1.img.xz"
    assert base_xz.exists()

    events: list[tuple[str, int]] = []

    def cb(stage: str, pct: int) -> None:
        events.append((stage, pct))

    out = build_provisioned(
        base_xz,
        "AGORA_FLEET_ID=p\nAGORA_FLEET_SECRET=q\n",
        tmp_path / "scratch",
        progress_cb=cb,
    )
    assert out.is_file()

    stages_seen = {stage for stage, _ in events}
    assert "decompressing" in stages_seen
    assert "compressing" in stages_seen

    decompress_pcts = [p for s, p in events if s == "decompressing"]
    compress_pcts = [p for s, p in events if s == "compressing"]

    # Watcher floor + final tick exist for each stage.
    assert decompress_pcts, "expected at least one decompressing tick"
    assert compress_pcts, "expected at least one compressing tick"

    # Monotonic non-decreasing within each stage.
    assert decompress_pcts == sorted(decompress_pcts)
    assert compress_pcts == sorted(compress_pcts)

    # In-bounds: decompress stays within 35..55, compress within 58..82
    # (matches the constants in ``cms/services/imager.py``).
    assert all(35 <= p <= 55 for p in decompress_pcts)
    assert all(58 <= p <= 82 for p in compress_pcts)

    # Compress stage starts after decompress finishes (callback ordering).
    decompress_idx = max(i for i, (s, _) in enumerate(events) if s == "decompressing")
    compress_idx = min(i for i, (s, _) in enumerate(events) if s == "compressing")
    assert decompress_idx < compress_idx


@needs_imager_tools
def test_build_provisioned_swallows_progress_callback_errors(tmp_path: Path):
    """A throwing progress callback never propagates into the build path.

    Progress is observability, not correctness -- a bad callback (e.g.
    a transient DB failure inside ``_set_progress``) must not crash
    the multi-minute imager pipeline and leave a half-built file.
    """
    base_img = _make_partitioned_fat_image(tmp_path / "agora-base-v1.img")
    subprocess.run(
        ["xz", "-z", "-1", "--keep", str(base_img)],
        check=True,
        capture_output=True,
    )
    base_xz = tmp_path / "agora-base-v1.img.xz"

    def boom(stage: str, pct: int) -> None:
        raise RuntimeError("simulated DB failure")

    out = build_provisioned(
        base_xz,
        "AGORA_FLEET_ID=p\nAGORA_FLEET_SECRET=q\n",
        tmp_path / "scratch",
        progress_cb=boom,
    )
    assert out.is_file()
    assert out.stat().st_size > 0


@needs_imager_tools
def test_build_provisioned_missing_base_raises(tmp_path: Path):
    with pytest.raises(ImagerError, match="base image not found"):
        build_provisioned(
            tmp_path / "does-not-exist.img.xz",
            "x=y\n",
            tmp_path / "scratch",
        )


def test_build_provisioned_no_tools_raises(tmp_path: Path, monkeypatch):
    # Force the discovery to find nothing.
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    base = tmp_path / "fake.img.xz"
    base.write_bytes(b"x")
    with pytest.raises(ImagerError, match="required tools"):
        build_provisioned(base, "x=y\n", tmp_path / "scratch")


def test_inject_fleet_env_no_tools_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    img = tmp_path / "fake.img"
    img.write_bytes(b"x")
    with pytest.raises(ImagerError, match="required tools"):
        inject_fleet_env(img, "x=y\n")
