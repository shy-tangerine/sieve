"""Atomic UTF-8 report replacement for developer tools.

Existing destination files, including symlinks, are replaced. Symlink targets
are never opened. The parent directory must already exist.
"""

import os
import tempfile
from pathlib import Path


def write_report(path: Path, text: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
