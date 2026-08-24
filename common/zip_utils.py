"""Safe ZIP extraction utilities shared across modules.

Both the plugin manager (untrusted user uploads) and the script service
(script archives) need path-traversal-safe extraction.  This module
provides a single implementation so the security logic is auditable in
one place.
"""
from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path


def safe_extract_zip(
    source: bytes | Path | zipfile.ZipFile,
    target_dir: Path,
    *,
    require_root_files: list[str] | None = None,
) -> zipfile.ZipFile:
    """Extract *source* into *target_dir*, blocking path-traversal attacks.

    Parameters
    ----------
    source:
        Raw ZIP bytes, a path to a ZIP file, or an already-opened
        ``ZipFile``.
    target_dir:
        Destination directory (must exist or will be created).
    require_root_files:
        If given, every name in this list must appear at the root level
        of the archive; otherwise ``ValueError`` is raised.

    Returns
    -------
    zipfile.ZipFile
        The opened archive (caller should ``close()`` if they need to
        inspect members afterwards).  When *source* is ``bytes`` the
        caller receives a *detached* archive backed by ``BytesIO``.

    Raises
    ------
    ValueError
        If a member has an absolute path, contains ``..`` components,
        or would extract outside *target_dir*; also raised when a
        required root file is missing.
    """
    close_archive = False
    if isinstance(source, zipfile.ZipFile):
        archive = source
    elif isinstance(source, bytes):
        archive = zipfile.ZipFile(io.BytesIO(source))
        close_archive = True
    else:
        archive = zipfile.ZipFile(source)
        close_archive = True

    try:
        names = archive.namelist()
        _validate_names(names)

        if require_root_files is not None:
            missing = [f for f in require_root_files if f not in names]
            if missing:
                raise ValueError(
                    f"ZIP missing required root file(s): {', '.join(missing)}"
                )

        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_target = target_dir.resolve()

        for name in names:
            out = (target_dir / name).resolve()
            # Belt-and-suspenders: verify resolution stays inside target
            if not str(out).startswith(str(resolved_target)):
                raise ValueError(f"ZIP member escapes target directory: {name}")

            if name.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, out.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

        return archive
    except Exception:
        if close_archive:
            archive.close()
        raise


def validate_zip_names(names: list[str]) -> None:
    """Reject absolute paths or ``..`` components in *names*.

    Raises ``ValueError`` with a descriptive message on the first
    offending entry.
    """
    _validate_names(names)


def _validate_names(names: list[str]) -> None:
    for name in names:
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts:
            raise ValueError(f"ZIP contains unsafe path: {name}")
