import os
import tempfile
from pathlib import Path
from typing import Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def atomic_write(path: Path, data: str) -> None:
    """Write data to a file atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_state(path: Path, model: Type[T]) -> T:
    """Read and parse a state JSON file. Returns default instance if missing."""
    try:
        return model.model_validate_json(path.read_text())
    except (FileNotFoundError, ValueError):
        return model()


def write_state(path: Path, state: BaseModel) -> None:
    """Write state to a JSON file atomically."""
    atomic_write(path, state.model_dump_json(indent=2))
