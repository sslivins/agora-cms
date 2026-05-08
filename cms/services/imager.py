"""Pi image provisioning pipeline.

Pure functions that take a base ``.img.xz`` and produce a per-fleet
provisioned ``.img.xz`` with an ``agora-fleet.env`` file dropped onto
the boot (FAT) partition. Used by the worker job handler that backs
the browser-driven imager flow.

The pipeline is intentionally subprocess-based:

* ``parted -s -j ... unit B print`` extracts the boot-partition byte
  offset from the partition table (no loop mounts, no root).
* ``mcopy`` (mtools) writes the env file into the FAT filesystem at
  that offset (no loop mounts, no root).
* ``xz`` decompresses the base image and recompresses the result.

All three tools must be installed in the runtime image (Dockerfile
adds ``mtools parted xz-utils``). On Windows the tools are absent and
the unit tests that touch them skip via :func:`shutil.which`.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Name of the env file dropped onto the boot partition. Matches the
# path the firmware reads at first boot
# (``/boot/firmware/agora-fleet.env``).
FLEET_ENV_FILENAME = "agora-fleet.env"

# Generous upper bound on legitimate fleet env payloads (the real ones
# are well under 1 KiB). Keeps abuse in check once this primitive is
# fed by API callers.
FLEET_ENV_MAX_BYTES = 64 * 1024

# Allowlist for caller-supplied output filenames. Must look like a
# plain ``foo.img.xz`` basename — no path separators, no ``..``.
_SAFE_OUTPUT_RE = re.compile(r"^[A-Za-z0-9._-]+\.img\.xz$")


def is_valid_output_name(name: str) -> bool:
    """Return ``True`` iff ``name`` is a safe ``.img.xz`` basename."""
    return bool(_SAFE_OUTPUT_RE.match(name or ""))


class ImagerError(RuntimeError):
    """Image build pipeline failure (parted/mtools/xz)."""


@dataclass(frozen=True)
class _Tools:
    parted: str
    mcopy: str
    xz: str

    @classmethod
    def discover(cls) -> "_Tools":
        missing = [t for t in ("parted", "mcopy", "xz") if shutil.which(t) is None]
        if missing:
            raise ImagerError(
                f"required tools not on PATH: {', '.join(missing)}. "
                "Install mtools, parted, and xz-utils."
            )
        return cls(
            parted=shutil.which("parted") or "parted",
            mcopy=shutil.which("mcopy") or "mcopy",
            xz=shutil.which("xz") or "xz",
        )


def _stderr_text(value: object) -> str:
    """Decode subprocess stderr regardless of text/bytes mode."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    if isinstance(value, str):
        return value.strip()
    return ""


def _run(cmd: list[str], *, stdin: bytes | None = None, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, capture text output, raise :class:`ImagerError` on failure."""
    try:
        result = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=stdin is None,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        stderr = _stderr_text(exc.stderr)
        raise ImagerError(
            f"command failed ({exc.returncode}): {' '.join(cmd)}\n{stderr}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ImagerError(f"command timed out: {' '.join(cmd)}") from exc
    return result


def parse_boot_partition_offset(parted_json: str) -> int:
    """Return the byte offset of the boot (first FAT) partition.

    ``parted_json`` is the stdout of ``parted -s -j <img> unit B print``.
    Accepts a few minor schema variations: ``size`` may end in ``B`` or
    ``b``, ``filesystem`` is matched case-insensitively against ``fat*``.
    """
    try:
        data = json.loads(parted_json)
    except json.JSONDecodeError as exc:
        raise ImagerError(f"could not parse parted JSON output: {exc}") from exc

    disk = data.get("disk")
    if not isinstance(disk, dict):
        raise ImagerError("parted JSON missing 'disk' object")

    partitions = disk.get("partitions") or []
    if not isinstance(partitions, list) or not partitions:
        raise ImagerError("parted JSON has no partitions")

    for part in partitions:
        if not isinstance(part, dict):
            continue
        fs = str(part.get("filesystem", "")).lower()
        if not fs.startswith("fat"):
            continue
        start = part.get("start")
        if start is None:
            # FAT partition with no offset is a parsing failure, not a
            # "skip and try the next one" situation — silently picking
            # a later partition could write the env into the wrong
            # filesystem.
            raise ImagerError("FAT partition is missing 'start' offset")
        return _parse_byte_value(start)

    raise ImagerError("no FAT partition found in parted output")


def _parse_byte_value(value: object) -> int:
    """Parse a parted byte value like ``'1048576B'`` into an int."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip().rstrip("Bb")
        try:
            return int(cleaned)
        except ValueError as exc:
            raise ImagerError(f"unparseable byte value: {value!r}") from exc
    raise ImagerError(f"unparseable byte value: {value!r}")


def boot_partition_offset(img_path: Path, *, tools: _Tools | None = None) -> int:
    """Return the byte offset of the boot partition in ``img_path``.

    Shells out to ``parted -s -j <img> unit B print`` and feeds the
    output to :func:`parse_boot_partition_offset`.
    """
    tools = tools or _Tools.discover()
    result = _run([tools.parted, "-s", "-j", str(img_path), "unit", "B", "print"])
    return parse_boot_partition_offset(result.stdout)


def inject_fleet_env(
    img_path: Path,
    fleet_env_text: str,
    *,
    boot_offset: int | None = None,
    tools: _Tools | None = None,
) -> None:
    """Drop ``agora-fleet.env`` into the boot partition of ``img_path``.

    Uses ``mcopy`` so the file is written in-place without root or loop
    mounts. ``boot_offset`` may be supplied (e.g. cached across calls);
    otherwise it is discovered via :func:`boot_partition_offset`.
    """
    payload = fleet_env_text.encode("utf-8")
    if b"\x00" in payload:
        raise ImagerError("fleet env contains a NUL byte")
    if len(payload) > FLEET_ENV_MAX_BYTES:
        raise ImagerError(
            f"fleet env is too large ({len(payload)} > {FLEET_ENV_MAX_BYTES} bytes)"
        )

    tools = tools or _Tools.discover()
    if boot_offset is None:
        boot_offset = boot_partition_offset(img_path, tools=tools)

    # ``mcopy -i <img>@@<offset> -o - ::/agora-fleet.env`` reads from
    # stdin and overwrites if the file exists. The ``::`` syntax is
    # mtools' drive-letter-free form.
    target = f"::/{FLEET_ENV_FILENAME}"
    _run(
        [tools.mcopy, "-i", f"{img_path}@@{boot_offset}", "-o", "-", target],
        stdin=payload,
    )


def read_fleet_env(
    img_path: Path,
    *,
    boot_offset: int | None = None,
    tools: _Tools | None = None,
) -> str:
    """Read back ``agora-fleet.env`` from the boot partition.

    Used by tests; not on the build hot path. Returns the file contents
    decoded as UTF-8.
    """
    tools = tools or _Tools.discover()
    if boot_offset is None:
        boot_offset = boot_partition_offset(img_path, tools=tools)
    target = f"::/{FLEET_ENV_FILENAME}"
    result = subprocess.run(
        [tools.mcopy, "-i", f"{img_path}@@{boot_offset}", "-n", target, "-"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ImagerError(f"mcopy read failed: {stderr}")
    return result.stdout.decode("utf-8")


def _xz_uncompressed_size(xz_bin: str, xz_path: Path) -> int | None:
    """Return the uncompressed size of ``xz_path`` in bytes, or ``None``.

    Uses ``xz --robot -l`` whose machine-readable ``totals`` line has
    the uncompressed-size in the 5th tab-separated field (1-indexed).
    Returns ``None`` on any failure -- callers fall back to no-progress
    mode which just shows the stage name without a percentage.
    """
    try:
        result = subprocess.run(
            [xz_bin, "--robot", "-l", str(xz_path)],
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("totals\t"):
            parts = line.split("\t")
            if len(parts) >= 5:
                try:
                    return int(parts[4])
                except ValueError:
                    return None
    return None


_ProgressCb = Callable[[str, int], None]


def _start_size_watcher(
    target_path: Path,
    expected_size: int,
    *,
    base_pct: int,
    end_pct: int,
    stage: str,
    progress_cb: _ProgressCb,
) -> tuple[threading.Thread, threading.Event]:
    """Spawn a daemon thread that polls ``target_path`` size at 1 Hz.

    Emits ``progress_cb(stage, pct)`` whenever the projected pct
    advances by at least 1.  Pct is clamped to ``[base_pct, end_pct - 1]``
    while the operation is running so we never report 100% before the
    underlying subprocess actually exits.

    The caller is responsible for setting the returned ``threading.Event``
    once the subprocess has finished; the thread will exit on the next
    tick (within 1 s).
    """
    stop = threading.Event()

    def _watch() -> None:
        last_pct = -1
        # Call once with the floor so the UI flips to the new stage
        # immediately, before the first second of polling has elapsed.
        try:
            progress_cb(stage, base_pct)
        except Exception:
            logger.debug("progress_cb floor emit raised", exc_info=True)
        last_pct = base_pct
        while not stop.is_set():
            try:
                cur = target_path.stat().st_size if target_path.exists() else 0
            except OSError:
                cur = 0
            span = end_pct - base_pct
            denom = max(1, expected_size)
            projected = base_pct + int(cur * span / denom)
            projected = max(base_pct, min(end_pct - 1, projected))
            if projected != last_pct:
                try:
                    progress_cb(stage, projected)
                except Exception:
                    logger.debug("progress_cb raised", exc_info=True)
                last_pct = projected
            if stop.wait(timeout=1.0):
                break

    t = threading.Thread(target=_watch, name=f"xz-progress-{stage}", daemon=True)
    t.start()
    return t, stop


def build_provisioned(
    base_xz_path: Path,
    fleet_env_text: str,
    scratch_dir: Path,
    *,
    output_name: str | None = None,
    tools: _Tools | None = None,
    progress_cb: _ProgressCb | None = None,
) -> Path:
    """Decompress, inject, recompress.

    Returns the path to the finished ``.img.xz`` inside ``scratch_dir``.
    The caller is responsible for cleaning up ``scratch_dir`` once the
    artifact has been uploaded. The intermediate raw ``.img`` (which
    contains the fleet secret) is removed unconditionally before this
    function returns or raises, so a crashed build does not leave
    unprotected secrets on disk.

    ``output_name`` must be a plain ``foo.img.xz`` basename -- no path
    separators, no ``..``. Path traversal is rejected.

    ``progress_cb`` is an optional ``(stage: str, pct: int) -> None``
    callback invoked from a background thread roughly once per second
    during the long-running xz decompress / compress phases.  Stages:

    * ``decompressing`` -- pct ramps 35..54 as the raw ``.img`` grows.
    * ``injecting``     -- single tick at 56 (mcopy is sub-second).
    * ``compressing``   -- pct ramps 58..81 as the recompressed
      ``.img.xz`` grows.

    Exceptions raised by ``progress_cb`` are swallowed -- progress is
    observability, not correctness.
    """
    if output_name is not None and not _SAFE_OUTPUT_RE.fullmatch(output_name):
        raise ImagerError(
            "invalid output_name; must match [A-Za-z0-9._-]+.img.xz"
        )

    tools = tools or _Tools.discover()
    if not base_xz_path.is_file():
        raise ImagerError(f"base image not found: {base_xz_path}")
    scratch_dir.mkdir(parents=True, exist_ok=True)

    base_name = base_xz_path.name
    if base_name.endswith(".img.xz"):
        stem = base_name[: -len(".img.xz")]
    elif base_name.endswith(".xz"):
        stem = base_name[: -len(".xz")]
    else:
        stem = base_name

    work_img = scratch_dir / f"{stem}.img"
    final_xz = scratch_dir / (output_name or f"{stem}.provisioned.img.xz")
    produced_xz = work_img.with_suffix(work_img.suffix + ".xz")

    # ----- progress configuration -----
    # The worker emits 35 ("building") and 85 ("uploading") around our
    # call site, so the xz stages live inside (35, 85).  Splitting it
    # 35..55 / 56 / 58..82 leaves a few points before the "uploading"
    # tick so the UI motion is always monotonic.
    DECOMPRESS_BASE = 35
    DECOMPRESS_END = 55
    INJECT_PCT = 56
    COMPRESS_BASE = 58
    COMPRESS_END = 82

    def _emit(stage: str, pct: int) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(stage, pct)
        except Exception:
            logger.debug("progress_cb raised", exc_info=True)

    expected_uncompressed = (
        _xz_uncompressed_size(tools.xz, base_xz_path) if progress_cb else None
    )
    expected_recompressed = base_xz_path.stat().st_size if progress_cb else 0

    try:
        # 1. Decompress base.xz -> work.img
        watcher_t: threading.Thread | None = None
        watcher_stop: threading.Event | None = None
        if progress_cb is not None and expected_uncompressed:
            watcher_t, watcher_stop = _start_size_watcher(
                work_img,
                expected_uncompressed,
                base_pct=DECOMPRESS_BASE,
                end_pct=DECOMPRESS_END,
                stage="decompressing",
                progress_cb=_emit,
            )
        else:
            _emit("decompressing", DECOMPRESS_BASE)

        try:
            with work_img.open("wb") as out_fh:
                proc = subprocess.Popen(
                    [tools.xz, "-d", "-c", "--threads=0", str(base_xz_path)],
                    stdout=out_fh,
                    stderr=subprocess.PIPE,
                )
                try:
                    _stdout, stderr = proc.communicate(timeout=1200)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise ImagerError("xz decompress timed out")
            if proc.returncode != 0:
                raise ImagerError(
                    f"xz decompress failed: {_stderr_text(stderr)}"
                )
        finally:
            if watcher_stop is not None:
                watcher_stop.set()
            if watcher_t is not None:
                watcher_t.join(timeout=2)

        _emit("decompressing", DECOMPRESS_END)

        # 2. Inject fleet env (sub-second, no watcher).
        _emit("injecting", INJECT_PCT)
        inject_fleet_env(work_img, fleet_env_text, tools=tools)

        # 3. Recompress at -1 (fastest reasonable) with all CPUs.
        # Pre-clear any stale outputs so retries in a reused scratch
        # dir don't trip xz's "already exists" guard.
        for stale in {produced_xz, final_xz}:
            if stale.exists():
                stale.unlink()

        watcher_t = None
        watcher_stop = None
        if progress_cb is not None and expected_recompressed > 0:
            watcher_t, watcher_stop = _start_size_watcher(
                produced_xz,
                expected_recompressed,
                base_pct=COMPRESS_BASE,
                end_pct=COMPRESS_END,
                stage="compressing",
                progress_cb=_emit,
            )
        else:
            _emit("compressing", COMPRESS_BASE)

        try:
            proc = subprocess.Popen(
                [tools.xz, "-z", "-1", "--threads=0", "--keep", str(work_img)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                _stdout, stderr = proc.communicate(timeout=1800)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise ImagerError("xz compress timed out")
            if proc.returncode != 0:
                raise ImagerError(
                    f"xz compress failed: {_stderr_text(stderr)}"
                )
        finally:
            if watcher_stop is not None:
                watcher_stop.set()
            if watcher_t is not None:
                watcher_t.join(timeout=2)

        _emit("compressing", COMPRESS_END)

        if produced_xz != final_xz:
            produced_xz.replace(final_xz)
        return final_xz
    finally:
        # The raw image carries fleet secrets -- never leave it behind.
        work_img.unlink(missing_ok=True)
