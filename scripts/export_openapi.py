"""Write the API's OpenAPI document to frontend/openapi.json.

    python scripts/export_openapi.py            # write it
    python scripts/export_openapi.py --check    # exit 1 if the file is stale

The web app's types (frontend/src/api/schema.d.ts) are generated from that
file with `npm run gen:api`; CI regenerates both and fails on any difference.
Keys are sorted and the output is stable, so a diff means the API changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGET = ROOT / "frontend" / "openapi.json"


def render() -> str:
    from app.main import create_app

    return json.dumps(create_app().openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=Path, default=TARGET)
    args = parser.parse_args(argv)
    document = render()
    if args.check:
        current = args.out.read_text() if args.out.exists() else ""
        if current != document:
            print(f"{args.out} is stale: run python scripts/export_openapi.py && (cd frontend && npm run gen:api)")
            return 1
        print(f"{args.out} is current")
        return 0
    args.out.write_text(document)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
