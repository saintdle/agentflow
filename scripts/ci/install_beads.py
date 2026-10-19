#!/usr/bin/env python3
"""Fetch a pinned official Beads executable and verify its release checksum."""

import argparse
import hashlib
from pathlib import Path
import platform
import tarfile
import urllib.request


def install(version: str, destination: Path) -> Path:
    if version not in {"1.1.0", "1.3.1"}:
        raise ValueError("only the integration and migration-fixture pins are allowed")
    system = {"Linux": "linux", "Darwin": "darwin"}[platform.system()]
    arch = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine()]
    archive_name = f"beads_{version}_{system}_{arch}.tar.gz"
    release = f"https://github.com/gastownhall/beads/releases/download/v{version}"
    destination.mkdir(parents=True, exist_ok=True)
    checksums = urllib.request.urlopen(f"{release}/checksums.txt", timeout=60).read().decode()
    expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1].lstrip("*") == archive_name)
    archive = destination / archive_name
    archive.write_bytes(urllib.request.urlopen(f"{release}/{archive_name}", timeout=120).read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f"checksum mismatch: {archive_name}")
    executable = destination / "bd"
    with tarfile.open(archive) as bundle:
        member = next(item for item in bundle.getmembers() if Path(item.name).name == "bd" and item.isfile())
        source = bundle.extractfile(member)
        if source is None:
            raise RuntimeError("release has no executable")
        executable.write_bytes(source.read())
    executable.chmod(0o755)
    return executable.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", choices=("1.1.0", "1.3.1"))
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(install(args.version, args.destination))
