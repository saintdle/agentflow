#!/usr/bin/env python3
"""Scan every reachable Git blob without printing matched secret material."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agentflow import publication


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--denylist-file", type=Path)
    parser.add_argument("--max-blob-bytes", type=int, default=10 * 1024 * 1024)
    args = parser.parse_args()
    try:
        markers = publication.denylist_from_environment(args.denylist_file)
        findings, blob_count = publication.scan_repository(
            args.root, deny_markers=markers, max_blob_bytes=args.max_blob_bytes
        )
    except publication.ScanError as exc:
        parser.error(str(exc))
    if findings:
        print(f"Historical privacy scan failed with {len(findings)} redacted finding(s):", file=sys.stderr)
        for finding in findings:
            print(
                f"- {finding.code}: object={finding.object_id[:12]} path={finding.path}",
                file=sys.stderr,
            )
        return 1
    print(f"Scanned {blob_count} unique blobs reachable from all refs: no private-data findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
