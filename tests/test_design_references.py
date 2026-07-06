#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_DIR / "scripts" / "run_prompt_pack.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_prompt_pack", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_pack_exposes_single_image_design_references():
    runner = load_runner()
    pack = runner.load_pack(SKILL_DIR / "prompt_packs" / "default-googleplay-prompt-pack")
    references = runner.design_references(pack)

    assert references
    first = references[0]
    assert first["id"] == "douyin-low-price-spotlight-popout"
    assert first["source_template"] == "douyin-low-price-card"
    assert "Google Play 1242x2208" in first["hard_constraints"]
    assert "screenshot_aspect_ratio_preserved" in first["quality_checks"]
    assert "spotlight_breaks_white_card" in first["quality_checks"]


if __name__ == "__main__":
    test_default_pack_exposes_single_image_design_references()
