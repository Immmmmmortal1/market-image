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


def test_default_preview_home_uses_auto_creative_set_not_template_picker():
    runner = load_runner()
    pack = runner.load_pack(SKILL_DIR / "prompt_packs" / "default-googleplay-prompt-pack")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        screenshots = []
        for index in range(1, 4):
            screenshot = tmp_path / f"{index:02d}.png"
            Image.new("RGB", (1080, 2375), "#f7f8fb").save(screenshot)
            screenshots.append(screenshot)
        state = {
            "pack": pack,
            "screenshots_dir": tmp_path,
            "screenshots": screenshots,
            "copy_map": {},
            "output_dir": tmp_path / "newImage",
            "width": 1242,
            "height": 2208,
            "jobs": [],
            "overrides": {},
            "selected_template_name": None,
            "auto_creative": True,
        }

        index_page = runner.render_auto_creative_preview_page(state)

    assert state["selected_template_name"] == "__auto_creative__"
    assert "随机创意市场图集合" in index_page
    assert "选择市场图模板" not in index_page
    assert len({job["template"] for job in state["jobs"]}) == 1
    assert state["campaign_template_name"] in {job["template"] for job in state["jobs"]}
    assert "统一视觉系统" in index_page


def test_auto_creative_campaign_uses_one_template_for_one_app_set():
    runner = load_runner()
    pack = runner.load_pack(SKILL_DIR / "prompt_packs" / "default-googleplay-prompt-pack")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        screenshots = []
        for index in range(1, 6):
            screenshot = tmp_path / f"{index:02d}.png"
            Image.new("RGB", (1080, 2375), "#4f63f3").save(screenshot)
            screenshots.append(screenshot)

        jobs = runner.build_auto_creative_jobs(pack, screenshots, tmp_path / "newImage", {})

    assert len(jobs) == 5
    assert len({job["template"] for job in jobs}) == 1


if __name__ == "__main__":
    test_default_preview_home_uses_auto_creative_set_not_template_picker()
