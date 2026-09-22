"""Build the ZIP attachment."""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

# Gmail rejects messages over 25 MB *after* MIME base64 encoding, which inflates
# attachments by ~37%. 17 MiB of files leaves comfortable headroom.
MAX_ZIP_BYTES = 17 * 1024 * 1024


@dataclass
class ZipResult:
    path: Path
    included: list[Path]
    too_large: list[Path]  # skipped because they would push the ZIP over the cap


def _unique_name(name: str, seen: set[str]) -> str:
    candidate, n = name, 1
    while candidate in seen:
        stem, dot, ext = name.rpartition(".")
        candidate = f"{stem} ({n}).{ext}" if dot else f"{name} ({n})"
        n += 1
    seen.add(candidate)
    return candidate


def build_zip(files: list[Path], dest: Path, max_bytes: int = MAX_ZIP_BYTES) -> ZipResult:
    """Zip `files` (in the given order) into `dest`. A file that would push the
    total past `max_bytes` is left out and reported; later, smaller files can
    still fit. An empty `files` list yields a valid empty ZIP."""
    included: list[Path] = []
    too_large: list[Path] = []
    total = 0
    for f in files:
        size = f.stat().st_size
        if total + size > max_bytes:
            too_large.append(f)
        else:
            total += size
            included.append(f)

    dest.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in included:
            zf.write(f, arcname=_unique_name(f.name, seen))
    return ZipResult(dest, included, too_large)
