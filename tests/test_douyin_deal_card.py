#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

from PIL import Image


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_DIR / "scripts" / "run_prompt_pack.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_prompt_pack", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_douyin_deal_card_preserves_google_play_canvas_and_screenshot_ratio():
    runner = load_runner()
    template = {
        "name": "douyin-low-price-card",
        "layout": "douyin-deal-card",
        "headline_small": "额外加料！",
        "headline_large": "同款更低价",
    }

    with tempfile.TemporaryDirectory() as tmp:
        screenshot_path = Path(tmp) / "01.png"
        Image.new("RGB", (1080, 2375), "#f7f8fb").save(screenshot_path)

        rendered = runner.render_html(
            screenshot_path=screenshot_path,
            template=template,
            copy="",
            width=1242,
            height=2208,
            index=1,
        )

    assert "width: 1242px;" in rendered
    assert "height: 2208px;" in rendered
    assert "--shot-width: 700px;" in rendered
    assert "--shot-height: 1539px;" in rendered
    assert "object-fit: contain;" in rendered
    assert "object-fit: cover;" not in rendered
    assert "width: var(--float-width);" in rendered
    assert "--float-width: calc(var(--card-width) + 132px);" in rendered
    assert "--float-height: 342px;" in rendered
    assert ".canvas > .float-deal" in rendered
    assert "overflow: visible;" in rendered
    assert '<div class="screen"><img src="' in rendered
    assert '<div class="float-deal">' in rendered


if __name__ == "__main__":
    test_douyin_deal_card_preserves_google_play_canvas_and_screenshot_ratio()
