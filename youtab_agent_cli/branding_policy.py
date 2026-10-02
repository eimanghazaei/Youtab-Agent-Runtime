"""Release/update policy for the permanently retired terminal logo."""
import os
from pathlib import Path

SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".cjs", ".mjs", ".json", ".yaml", ".yml", ".toml", ".txt", ".svg"}
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "tests", "__tests__", "e2e", "docs", "_evidence", "_backups"}
# Keep a signature, not a copy of the retired six-line artwork.
_RETIRED_PREFIX = "\u2588\u2588\u2557  \u2588\u2588\u2557" + "\u2588" * 7 + "\u2557" + "\u2588" * 6 + "\u2557 " + "\u2588" * 3 + "\u2557"


def contains_retired_banner(raw: bytes) -> bool:
    """Recognize source and compiler-escaped versions of the retired logo."""
    return (_RETIRED_PREFIX.encode("utf-8") in raw
            or _RETIRED_PREFIX.encode("unicode_escape") in raw)


def find_retired_banner(root: Path) -> Path | None:
    """Inspect product sources; dependencies and preserved evidence are excluded."""
    def fail_read(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, onerror=fail_read):
        dirs[:] = sorted(name for name in dirs if name not in _SKIP_DIRS)
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() in SOURCE_SUFFIXES and contains_retired_banner(path.read_bytes()):
                return path
    return None
