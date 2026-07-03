from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import Settings
from .pipeline import TeamKnowParsePipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Run TeamKnowParse on a file or folder.")
    parser.add_argument("input_path", type=Path)
    parser.add_argument("--out", type=Path, default=Path("runs"))
    parser.add_argument("--mode", choices=["txt", "mixed"], default="mixed")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    args = parser.parse_args()
    tags = [tag.strip() for tag in args.tags.split(",") if tag.strip()]
    pipeline = TeamKnowParsePipeline(Settings())
    run_dir = asyncio.run(pipeline.run(args.input_path, args.out, mode=args.mode, tags=tags))
    print(run_dir)


if __name__ == "__main__":
    main()

