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
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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


def build_provisioned(
    base_xz_path: Path,
    fleet_env_text: str,
    scratch_dir: Path,
    *,
    output_name: str | None = None,
    tools: _Tools | None = None,
) -> Path:
    """Decompress, inject, recompress.

    Returns the path to the finished ``.img.xz`` inside ``scratch_dir``.
    The caller is responsible for cleaning up ``scratch_dir`` once the
    artifact has been uploaded. The intermediate raw ``.img`` (which
    contains the fleet secret) is removed unconditionally before this
    function returns or raises, so a crashed build does not leave
    unprotected secrets on disk.

    ``output_name`` must be a plain ``foo.img.xz`` basename — no path
    separators, no ``..``. Path traversal is rejected.
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

    try:
        # 1. Decompress base.xz → work.img
        with work_img.open("wb") as out_fh:
            try:
                subprocess.run(
                    [tools.xz, "-d", "-c", "--threads=0", str(base_xz_path)],
                    stdout=out_fh,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=1200,
                )
            except subprocess.CalledProcessError as exc:
                raise ImagerError(
                    f"xz decompress failed: {_stderr_text(exc.stderr)}"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise ImagerError("xz decompress timed out") from exc

        # 2. Inject fleet env
        inject_fleet_env(work_img, fleet_env_text, tools=tools)

        # 3. Recompress at -1 (fastest reasonable) with all CPUs.
        # Pre-clear any stale outputs so retries in a reused scratch
        # dir don't trip xz's "already exists" guard.
        for stale in {produced_xz, final_xz}:
            if stale.exists():
                stale.unlink()
        try:
            subprocess.run(
                [tools.xz, "-z", "-1", "--threads=0", "--keep", str(work_img)],
                check=True,
                capture_output=True,
                timeout=1800,
            )
        except subprocess.CalledProcessError as exc:
            raise ImagerError(
                f"xz compress failed: {_stderr_text(exc.stderr)}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ImagerError("xz compress timed out") from exc

        if produced_xz != final_xz:
            produced_xz.replace(final_xz)
        return final_xz
    finally:
        # The raw image carries fleet secrets — never leave it behind.
        work_img.unlink(missing_ok=True)
