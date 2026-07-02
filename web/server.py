#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_prompt_pack.py"


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the market image preview web service.")
    parser.add_argument("--screenshots-dir", required=True, help="Folder containing numbered screenshots such as 01.png or 01.jpg.")
    parser.add_argument("--prompt-pack-dir", help="Optional prompt pack directory. Defaults to the built-in pack.")
    parser.add_argument("--output-dir", help="Optional output directory. Defaults to <screenshots-dir>/newImage.")
    parser.add_argument("--host", default="127.0.0.1", help="Preview server host.")
    parser.add_argument("--port", default="8765", help="Preview server port.")
    parser.add_argument("--open", action="store_true", help="Open the preview URL in the default browser before blocking.")
    args = parser.parse_args()

    if not RUNNER.exists():
        raise FileNotFoundError(f"Runner not found: {RUNNER}")

    cmd = [
        sys.executable,
        str(RUNNER),
        "--screenshots-dir",
        args.screenshots_dir,
        "--serve-preview",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.prompt_pack_dir:
        cmd.extend(["--prompt-pack-dir", args.prompt_pack_dir])
    if args.output_dir:
        cmd.extend(["--output-dir", args.output_dir])

    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}/")

    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
