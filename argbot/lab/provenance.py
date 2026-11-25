"""Proveniencia pública: código y dependencias; nunca entorno, claves o estado privado."""
import hashlib
from importlib.metadata import distributions
import json
from pathlib import Path
import platform
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def source_hash(path):
    # Git puede convertir LF a CRLF: la identidad del código normaliza solo EOL.
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def provenance(root=ROOT):
    def git(*args):
        try:
            return subprocess.check_output(
                ["git", "--no-optional-locks", "-c", "core.excludesFile=", *args],
                cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    top = git("rev-parse", "--show-toplevel")
    # A copy extracted inside another Git checkout must not report that checkout's revision.
    revision = git("rev-parse", "HEAD") if top and Path(top).resolve() == root.resolve() else None
    status = git("status", "--porcelain", "--untracked-files=normal") if revision else None
    files = sorted((root / "argbot").rglob("*.py"))
    files += [root / name for name in ("pyproject.toml",) if (root / name).exists()]
    hashes = {p.relative_to(root).as_posix(): source_hash(p) for p in files}
    return {"git_revision": revision, "git_dirty": bool(status) if status is not None else None,
            "git_changed_entries": len(status.splitlines()) if status else 0 if status == "" else None,
            "source_hash_convention": "SHA256 UTF-8 bytes with CRLF normalized to LF",
            "source_files": hashes, "source_tree_sha256": object_hash(hashes),
            "python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.system(), "machine": platform.machine(),
            "dependencies": sorted({(d.metadata["Name"], d.version) for d in distributions()
                                     if d.metadata["Name"]})}
