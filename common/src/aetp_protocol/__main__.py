"""Generate the AETP V2 JSON Schema snapshot.

Usage:
    python -m aetp_protocol <output.json>
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .schema import write_v2_schema_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AETP V2 JSON Schema")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    write_v2_schema_snapshot(args.output)


if __name__ == "__main__":
    main()
