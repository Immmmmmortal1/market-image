#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import functools
import html
import http.server
import json
import mimetypes
import re
import shutil
import socketserver
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


SKILL_DIR = Path(__file__).resolve().parents[1]
PROMPT_PACKS_DIR = SKILL_DIR / "prompt_packs"
DEFAULT_PACK_DIR = PROMPT_PACKS_DIR / "default-googleplay-prompt-pack"
DEFAULT_CANVAS = {"width": 1242, "height": 2208}
DEFAULT_PREVIEW_HOST = "127.0.0.1"
DEFAULT_PREVIEW_PORT = 8765
OVERRIDES_FILENAME = "overrides.json"
CHROME_CANDIDATES = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
]


def load_pack(pack_dir: Path) -> dict:
    pack_path = pack_dir / "pack.json"
    if not pack_path.exists():
        raise RuntimeError(f"Prompt pack is missing pack.json: {pack_dir}")
    data = json.loads(pack_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("pack.json must be a JSON object")
    if data.get("mode") != "html_screenshot_generation":
        raise RuntimeError("pack.json mode must be html_screenshot_generation")
    templates = data.get("templates")
    if not isinstance(templates, list) or not templates:
        raise RuntimeError("pack.json must include non-empty templates")
    for item in templates:
        if not isinstance(item, dict) or not item.get("name"):
            raise RuntimeError("Each prompt template must include name")
    return data


def canvas_size(pack: dict) -> tuple[int, int]:
    canvas = pack.get("canvas") or DEFAULT_CANVAS
    width = int(canvas.get("width") or DEFAULT_CANVAS["width"])
    height = int(canvas.get("height") or DEFAULT_CANVAS["height"])
    if width <= 0 or height <= 0:
        raise RuntimeError("canvas.width and canvas.height must be positive")
    return width, height


def list_prompt_packs() -> list[dict]:
    packs: list[dict] = []
    if not PROMPT_PACKS_DIR.exists():
        return packs
    for pack_dir in sorted(PROMPT_PACKS_DIR.iterdir()):
        if not pack_dir.is_dir() or not (pack_dir / "pack.json").exists():
            continue
        try:
            pack = load_pack(pack_dir)
            width, height = canvas_size(pack)
            packs.append(
                {
                    "pack_name": pack.get("pack_name") or pack_dir.name,
                    "path": str(pack_dir),
                    "default": pack_dir.resolve() == DEFAULT_PACK_DIR.resolve(),
                    "mode": pack.get("mode"),
                    "canvas": {"width": width, "height": height},
                    "template_count": len(pack.get("templates") or []),
                    "notes": pack.get("notes") or "",
                }
            )
        except Exception as exc:
            packs.append({"path": str(pack_dir), "error": str(exc)})
    return packs


def sorted_input_images(folder: Path) -> list[Path]:
    if not folder.exists() or not folder.is_dir():
        raise RuntimeError(f"Screenshots directory does not exist: {folder}")
    numbered: list[tuple[int, Path]] = []
    for p in folder.iterdir():
        if not p.is_file() or p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            continue
        match = re.fullmatch(r"(\d+)", p.stem)
        if not match:
            continue
        numbered.append((int(match.group(1)), p))
    return [p for _, p in sorted(numbered, key=lambda item: item[0])]


def load_copy_map(copy_json: str | None, copy_file: str | None) -> dict[str, str]:
    if copy_json and copy_file:
        raise RuntimeError("Use only one of --copy-json or --copy-file")
    if copy_file:
        data = json.loads(Path(copy_file).expanduser().read_text(encoding="utf-8"))
    elif copy_json:
        data = json.loads(copy_json)
    else:
        return {}
    if not isinstance(data, dict):
        raise RuntimeError("Copy values must be a JSON object")
    return {str(key): str(value) for key, value in data.items()}


def copy_for_image(copy_map: dict[str, str], image_path: Path, index: int) -> str:
    keys = [image_path.stem, str(index), f"{index:02d}", image_path.name, "default"]
    for key in keys:
        if key in copy_map:
            return copy_map[key]
    return ""


def split_copy(copy: str, default_small: str, default_large: str) -> tuple[str, str]:
    value = copy.strip()
    if not value:
        return default_small, default_large
    parts = [part.strip() for part in re.split(r"\n|\|", value) if part.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return default_small, parts[0]


def load_overrides(output_dir: Path) -> dict[str, dict]:
    path = output_dir / OVERRIDES_FILENAME
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{OVERRIDES_FILENAME} must be a JSON object")
    cleaned: dict[str, dict] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            cleaned[str(key)] = sanitize_override(value)
    return cleaned


def save_overrides(output_dir: Path, overrides: dict[str, dict]) -> Path:
    path = output_dir / OVERRIDES_FILENAME
    path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def clamp_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except Exception:
        return default
    return max(minimum, min(maximum, number))


def sanitize_color(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw
    return ""


def sanitize_override(value: dict) -> dict:
    return {
        "title_top": str(value.get("title_top") or "").strip(),
        "title_bottom": str(value.get("title_bottom") or "").strip(),
        "background_color": sanitize_color(value.get("background_color")),
        "screenshot_x": clamp_float(value.get("screenshot_x"), 0, -800, 800),
        "screenshot_y": clamp_float(value.get("screenshot_y"), 0, -800, 800),
        "screenshot_scale": clamp_float(value.get("screenshot_scale"), 1, 0.5, 2.0),
    }


def override_for_index(overrides: dict[str, dict], index: int) -> dict:
    return overrides.get(f"{index:02d}") or overrides.get(str(index)) or {}


def default_titles(template: dict, copy: str, index: int) -> tuple[str, str]:
    layout = str(template.get("layout") or "clean-discover-phone")
    if layout == "clean-discover-phone":
        return split_copy(
            copy,
            str(template.get("headline_small") or "发现"),
            str(template.get("headline_large") or "多元兴趣方式"),
        )
    if layout == "purple-live-phone":
        return split_copy(
            copy,
            str(template.get("headline_small") or "发现新直播"),
            str(template.get("headline_large") or "剪辑与故事"),
        )
    if layout == "spotify-pink-series":
        _, headline, eyebrow, _, _ = spotify_defaults(template, index)
        if copy.strip():
            parts = [part.strip() for part in re.split(r"\n|\|", copy.strip()) if part.strip()]
            if len(parts) >= 2:
                return parts[0], parts[1]
            return "", parts[0]
        return eyebrow, headline
    if layout == "element-dark-glow-series":
        variant = element_defaults(template, index)
        if variant["kind"] == "icon":
            top = variant["headline_top"] or "The\nfastest"
            bottom = variant["headline_bottom"] or "Element\never."
            return top, bottom
        headline = copy.strip() or variant["headline"]
        if variant["caption_position"] == "top":
            return headline, ""
        return "", headline
    return "", copy


def copy_with_override(template: dict, copy: str, index: int, override: dict) -> str:
    if not override:
        return copy
    current_top, current_bottom = default_titles(template, copy, index)
    top = override.get("title_top") or current_top
    bottom = override.get("title_bottom") or current_bottom
    if top and bottom:
        return f"{top}|{bottom}"
    return top or bottom or copy


def inject_override_css(rendered: str, override: dict) -> str:
    if not override:
        return rendered
    css: list[str] = []
    background = override.get("background_color") or ""
    if background:
        css.append(f".canvas {{ background: {background} !important; }}")
        css.append(f".poster {{ background: {background} !important; }}")
    x = clamp_float(override.get("screenshot_x"), 0, -800, 800)
    y = clamp_float(override.get("screenshot_y"), 0, -800, 800)
    scale = clamp_float(override.get("screenshot_scale"), 1, 0.5, 2.0)
    if x or y or scale != 1:
        css.append(
            ".screen img { "
            f"transform: translate({x}px, {y}px) scale({scale}); "
            "transform-origin: center top; "
            "}"
        )
    if not css:
        return rendered
    block = "\n  <style id=\"user-overrides\">\n    " + "\n    ".join(css) + "\n  </style>"
    return rendered.replace("</head>", f"{block}\n</head>")


def image_data_uri(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    mime_type = mime_type or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def render_clean_discover_html(
    *,
    screenshot_path: Path,
    template: dict,
    copy: str,
    width: int,
    height: int,
) -> str:
    default_small = str(template.get("headline_small") or "发现")
    default_large = str(template.get("headline_large") or "多元兴趣方式")
    small, large = split_copy(copy, default_small, default_large)
    img_src = image_data_uri(screenshot_path)
    small_html = html.escape(small)
    large_html = html.escape(large)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width={width}, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background: #f7f8fa;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    .canvas {{
      position: relative;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background:
        radial-gradient(circle at 50% 52%, rgba(0, 0, 0, 0.055), rgba(0, 0, 0, 0) 34%),
        linear-gradient(180deg, #ffffff 0%, #f5f6f8 72%, #eef0f3 100%);
    }}
    .copy {{
      position: absolute;
      top: 118px;
      left: 0;
      width: 100%;
      text-align: center;
      color: #202124;
      letter-spacing: -0.03em;
      z-index: 2;
    }}
    .copy-small {{
      font-size: 56px;
      line-height: 1.2;
      font-weight: 400;
      margin-bottom: 22px;
    }}
    .copy-large {{
      font-size: 82px;
      line-height: 1.08;
      font-weight: 800;
    }}
    .phone-shadow {{
      position: absolute;
      left: 50%;
      top: 482px;
      width: 760px;
      height: 1570px;
      transform: translateX(-50%);
      border-radius: 92px;
      background: rgba(0, 0, 0, 0.06);
      filter: blur(28px);
      z-index: 0;
    }}
    .phone {{
      position: absolute;
      left: 50%;
      top: 454px;
      width: 760px;
      height: 1588px;
      transform: translateX(-50%);
      border-radius: 92px;
      background: #ffffff;
      box-shadow:
        0 34px 80px rgba(27, 31, 36, 0.14),
        inset 0 0 0 10px rgba(255, 255, 255, 0.95),
        inset 0 0 0 16px rgba(232, 235, 239, 0.82);
      z-index: 1;
    }}
    .screen {{
      position: absolute;
      left: 55px;
      top: 58px;
      width: 650px;
      height: 1430px;
      border-radius: 52px;
      overflow: hidden;
      background: #ffffff;
    }}
    .screen img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      object-position: center top;
      display: block;
    }}
    .glass {{
      pointer-events: none;
      position: absolute;
      inset: 0;
      border-radius: 92px;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.95);
    }}
  </style>
</head>
<body>
  <main class="canvas">
    <section class="copy">
      <div class="copy-small">{small_html}</div>
      <div class="copy-large">{large_html}</div>
    </section>
    <div class="phone-shadow"></div>
    <section class="phone" aria-label="phone mockup">
      <div class="screen"><img src="{img_src}" alt=""></div>
      <div class="glass"></div>
    </section>
  </main>
</body>
</html>
"""


def render_purple_live_html(
    *,
    screenshot_path: Path,
    template: dict,
    copy: str,
    width: int,
    height: int,
) -> str:
    default_small = str(template.get("headline_small") or "发现新直播")
    default_large = str(template.get("headline_large") or "剪辑与故事")
    small, large = split_copy(copy, default_small, default_large)
    img_src = image_data_uri(screenshot_path)
    small_html = html.escape(small)
    large_html = html.escape(large)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width={width}, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background: #a94cff;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    .canvas {{
      position: relative;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background:
        radial-gradient(circle at 18% 26%, rgba(255,255,255,.42), rgba(255,255,255,0) 16%),
        radial-gradient(circle at 88% 40%, rgba(255,255,255,.34), rgba(255,255,255,0) 20%),
        radial-gradient(circle at 48% 76%, rgba(255,0,220,.20), rgba(255,0,220,0) 30%),
        linear-gradient(180deg, #e87cf5 0%, #ba63ff 49%, #8948ff 100%);
    }}
    .headline {{
      position: absolute;
      top: 148px;
      left: 0;
      width: 100%;
      text-align: center;
      color: #111014;
      font-weight: 500;
      line-height: 1.08;
      letter-spacing: -0.035em;
      z-index: 5;
    }}
    .headline div {{
      font-size: 56px;
    }}
    .star {{
      position: absolute;
      background: #fff;
      filter: drop-shadow(0 0 20px rgba(255,255,255,.72));
      clip-path: polygon(50% 0%, 62% 35%, 100% 50%, 62% 65%, 50% 100%, 38% 65%, 0% 50%, 38% 35%);
      z-index: 2;
    }}
    .star.big {{ left: 92px; top: 84px; width: 126px; height: 126px; transform: rotate(8deg); }}
    .star.mid {{ left: 252px; top: 102px; width: 72px; height: 72px; transform: rotate(12deg); }}
    .star.small {{ left: 130px; top: 232px; width: 58px; height: 58px; transform: rotate(-8deg); }}
    .orb {{
      position: absolute;
      right: -158px;
      top: 160px;
      width: 350px;
      height: 350px;
      border-radius: 50%;
      background: radial-gradient(circle at 36% 28%, #fff, #e5e7ee 58%, #cfd4df 100%);
      box-shadow: -22px 28px 60px rgba(105, 72, 173, .28);
      z-index: 1;
    }}
    .orb::after {{
      content: "";
      position: absolute;
      left: -68px;
      top: 190px;
      width: 240px;
      height: 240px;
      border-radius: 50%;
      background: radial-gradient(circle at 36% 28%, #fff, #eceff5 58%, #d6dbe4 100%);
    }}
    .phone {{
      position: absolute;
      left: 50%;
      top: 416px;
      width: 742px;
      height: 1580px;
      transform: translateX(-50%);
      border-radius: 62px;
      background: #08070b;
      border: 10px solid #00f5dc;
      box-shadow:
        0 44px 110px rgba(37, 0, 94, .44),
        0 0 0 2px rgba(255,255,255,.18) inset;
      overflow: hidden;
      z-index: 4;
    }}
    .screen {{
      position: absolute;
      left: 0;
      top: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      border-radius: 51px;
      background: #050507;
    }}
    .screen img {{
      display: block;
      width: 100%;
      height: 100%;
      object-fit: cover;
      object-position: center top;
      filter: contrast(1.03) saturate(1.03);
    }}
    .live-badge {{
      position: absolute;
      right: -118px;
      top: 980px;
      width: 456px;
      height: 168px;
      border-radius: 46px;
      transform: rotate(11deg);
      z-index: 8;
      background: linear-gradient(135deg, #ff27c3 0%, #a64cff 56%, #5832e6 100%);
      box-shadow:
        0 38px 70px rgba(62, 0, 128, .32),
        inset 0 6px 14px rgba(255,255,255,.38),
        inset 0 -14px 24px rgba(43,0,115,.32);
      color: white;
      font-size: 86px;
      line-height: 168px;
      font-weight: 800;
      text-align: center;
      letter-spacing: .02em;
      text-shadow: 0 5px 0 rgba(50, 36, 128, .42);
    }}
    .spark-line {{
      position: absolute;
      right: 88px;
      top: 956px;
      width: 330px;
      height: 2px;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,.92), transparent);
      z-index: 7;
    }}
    .spark-line::after {{
      content: "";
      position: absolute;
      right: 80px;
      top: -24px;
      width: 50px;
      height: 50px;
      background: #fff;
      filter: drop-shadow(0 0 22px #fff);
      clip-path: polygon(50% 0%, 60% 40%, 100% 50%, 60% 60%, 50% 100%, 40% 60%, 0% 50%, 40% 40%);
    }}
    .mixer {{
      position: absolute;
      right: -118px;
      bottom: -44px;
      width: 442px;
      height: 242px;
      border-radius: 56px;
      transform: rotate(-14deg);
      z-index: 9;
      background:
        linear-gradient(135deg, rgba(255,255,255,.88), rgba(255,255,255,.28) 32%, rgba(0,0,0,.14) 33%),
        linear-gradient(180deg, #2d2b31, #08080b 58%, #e8e1d6 62%, #ffffff 100%);
      box-shadow: 0 34px 62px rgba(44, 0, 84, .35);
    }}
    .knob {{
      position: absolute;
      right: 52px;
      top: 22px;
      width: 88px;
      height: 88px;
      border-radius: 50%;
      background: radial-gradient(circle at 36% 30%, #9b94a4, #24232a 70%);
      box-shadow: 0 0 0 10px #a345ff;
    }}
    .pad {{
      position: absolute;
      width: 86px;
      height: 48px;
      border-radius: 14px;
      background: linear-gradient(180deg, #ddd, #8d9198);
      box-shadow: inset 0 5px 8px rgba(255,255,255,.48), 0 7px 0 rgba(0,0,0,.22);
    }}
    .pad.p1 {{ left: 68px; top: 118px; }}
    .pad.p2 {{ left: 180px; top: 92px; }}
    .pad.p3 {{ left: 176px; top: 168px; }}
    .pad.p4 {{ left: 292px; top: 145px; }}
  </style>
</head>
<body>
  <main class="canvas">
    <div class="star big"></div>
    <div class="star mid"></div>
    <div class="star small"></div>
    <div class="orb"></div>
    <section class="headline">
      <div>{small_html}</div>
      <div>{large_html}</div>
    </section>
    <section class="phone">
      <div class="screen"><img src="{img_src}" alt=""></div>
    </section>
    <div class="spark-line"></div>
    <div class="live-badge">LIVE</div>
    <div class="mixer">
      <div class="knob"></div>
      <div class="pad p1"></div>
      <div class="pad p2"></div>
      <div class="pad p3"></div>
      <div class="pad p4"></div>
    </div>
  </main>
</body>
</html>
"""


def spotify_defaults(template: dict, index: int) -> tuple[str, str, str, str, str]:
    variants = template.get("variants")
    selected = None
    if isinstance(variants, list) and variants:
        selected = variants[min(index - 1, len(variants) - 1)]
    if not isinstance(selected, dict):
        selected = {}
    brand = str(selected.get("brand") or template.get("brand") or "Spotify")
    headline = str(selected.get("headline") or template.get("headline") or "音乐和播客\n一网打尽")
    eyebrow = str(selected.get("eyebrow") or "")
    tilt = str(selected.get("tilt") or "-1.5deg")
    phone_top = str(selected.get("phone_top") or "520px")
    return brand, headline, eyebrow, tilt, phone_top


def render_spotify_pink_series_html(
    *,
    screenshot_path: Path,
    template: dict,
    copy: str,
    width: int,
    height: int,
    index: int,
) -> str:
    brand, headline, eyebrow, tilt, phone_top = spotify_defaults(template, index)
    value = copy.strip()
    if value:
        parts = [part.strip() for part in re.split(r"\n|\|", value) if part.strip()]
        if len(parts) >= 2:
            eyebrow = parts[0]
            headline = parts[1]
        else:
            headline = parts[0]
    img_src = image_data_uri(screenshot_path)
    brand_html = html.escape(brand)
    headline_html = "<br>".join(html.escape(part) for part in headline.splitlines() if part.strip())
    eyebrow_html = html.escape(eyebrow)
    show_brand = "1" if index == 1 else "0"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width={width}, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background: #5fb28f;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    .canvas {{
      position: relative;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background:
        radial-gradient(circle at 0% 42%, rgba(255, 122, 189, .72), rgba(255, 122, 189, 0) 19%),
        radial-gradient(circle at 100% 58%, rgba(255, 122, 189, .72), rgba(255, 122, 189, 0) 19%),
        linear-gradient(90deg, #5ab288 0%, #f58ac3 12%, #f58ac3 88%, #5ab288 100%);
    }}
    .poster {{
      position: absolute;
      left: 116px;
      top: 0;
      width: 1010px;
      height: 100%;
      background: #f58ac3;
      overflow: hidden;
      box-shadow: 0 0 70px rgba(0,0,0,.08);
    }}
    .copy {{
      position: absolute;
      left: 0;
      top: 126px;
      width: 100%;
      text-align: center;
      color: #151515;
      z-index: 4;
    }}
    .brand {{
      display: {"block" if show_brand == "1" else "none"};
      margin-bottom: 22px;
      font-size: 36px;
      line-height: 1;
      font-weight: 700;
    }}
    .brand-mark {{
      display: inline-block;
      width: 38px;
      height: 38px;
      margin-right: 8px;
      vertical-align: -7px;
      border-radius: 50%;
      background:
        radial-gradient(circle at 50% 50%, transparent 0 46%, #111 47% 100%);
      position: relative;
    }}
    .brand-mark::before,
    .brand-mark::after {{
      content: "";
      position: absolute;
      left: 8px;
      right: 8px;
      height: 4px;
      border-radius: 999px;
      background: #f58ac3;
      transform: rotate(8deg);
    }}
    .brand-mark::before {{ top: 12px; box-shadow: 0 7px 0 #f58ac3; }}
    .brand-mark::after {{ top: 26px; width: 16px; right: auto; }}
    .eyebrow {{
      font-size: 34px;
      font-weight: 500;
      line-height: 1.2;
      margin-bottom: 10px;
    }}
    .headline {{
      font-size: 54px;
      font-weight: 800;
      line-height: 1.08;
      letter-spacing: -0.04em;
    }}
    .phone-shadow {{
      position: absolute;
      left: 50%;
      top: calc({phone_top} + 46px);
      width: 700px;
      height: 1340px;
      transform: translateX(-50%) rotate({tilt});
      border-radius: 70px;
      background: rgba(0,0,0,.24);
      filter: blur(38px);
      z-index: 1;
    }}
    .phone {{
      position: absolute;
      left: 50%;
      top: {phone_top};
      width: 694px;
      height: 1412px;
      transform: translateX(-50%) rotate({tilt});
      border-radius: 72px;
      background: #070707;
      box-shadow:
        0 30px 70px rgba(0,0,0,.34),
        inset 0 0 0 14px #090909,
        inset 0 0 0 18px rgba(255,255,255,.05);
      overflow: hidden;
      z-index: 2;
    }}
    .speaker {{
      position: absolute;
      left: 50%;
      top: 19px;
      width: 96px;
      height: 18px;
      transform: translateX(-50%);
      border-radius: 999px;
      background: #111;
      z-index: 5;
    }}
    .screen {{
      position: absolute;
      left: 26px;
      top: 44px;
      width: 642px;
      height: 1322px;
      border-radius: 46px;
      overflow: hidden;
      background: #050505;
    }}
    .screen img {{
      display: block;
      width: 100%;
      height: 100%;
      object-fit: cover;
      object-position: center top;
    }}
  </style>
</head>
<body>
  <main class="canvas">
    <section class="poster">
      <section class="copy">
        <div class="brand"><span class="brand-mark"></span>{brand_html}</div>
        <div class="eyebrow">{eyebrow_html}</div>
        <div class="headline">{headline_html}</div>
      </section>
      <div class="phone-shadow"></div>
      <section class="phone">
        <div class="speaker"></div>
        <div class="screen"><img src="{img_src}" alt=""></div>
      </section>
    </section>
  </main>
</body>
</html>
"""


def element_defaults(template: dict, index: int) -> dict:
    variants = template.get("variants")
    selected = None
    if isinstance(variants, list) and variants:
        selected = variants[min(index - 1, len(variants) - 1)]
    if not isinstance(selected, dict):
        selected = {}
    return {
        "kind": str(selected.get("kind") or "phone"),
        "headline": str(selected.get("headline") or "Secure and\nencrypted."),
        "headline_top": str(selected.get("headline_top") or ""),
        "headline_bottom": str(selected.get("headline_bottom") or ""),
        "caption_position": str(selected.get("caption_position") or "bottom"),
        "tilt": str(selected.get("tilt") or "0deg"),
        "phone_top": str(selected.get("phone_top") or "442px"),
        "phone_width": str(selected.get("phone_width") or "672px"),
        "phone_height": str(selected.get("phone_height") or "1432px"),
        "screen_bg": str(selected.get("screen_bg") or "#10161c"),
    }


def render_element_dark_glow_series_html(
    *,
    screenshot_path: Path,
    template: dict,
    copy: str,
    width: int,
    height: int,
    index: int,
) -> str:
    variant = element_defaults(template, index)
    headline = copy.strip() or variant["headline"]
    top_headline = variant["headline_top"]
    bottom_headline = variant["headline_bottom"]
    if copy.strip():
        parts = [part.strip() for part in re.split(r"\n\n+|\|", copy.strip()) if part.strip()]
        if len(parts) >= 2:
            top_headline = parts[0]
            bottom_headline = parts[1]
        else:
            headline = parts[0]
    if variant["kind"] == "icon":
        if not top_headline:
            top_headline = "The\nfastest"
        if not bottom_headline:
            bottom_headline = "Element\never."
    else:
        if variant["caption_position"] == "top":
            top_headline = headline
            bottom_headline = ""
        else:
            top_headline = ""
            bottom_headline = headline
    top_headline_html = "<br>".join(html.escape(part) for part in top_headline.splitlines() if part.strip())
    bottom_headline_html = "<br>".join(html.escape(part) for part in bottom_headline.splitlines() if part.strip())
    img_src = image_data_uri(screenshot_path)
    kind = variant["kind"]
    top_caption = "block" if top_headline.strip() else "none"
    bottom_caption = "block" if bottom_headline.strip() else "none"
    icon_display = "block" if kind == "icon" else "none"
    phone_display = "block" if kind != "icon" else "none"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width={width}, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background: #0b1015;
      font-family: -apple-system, BlinkMacSystemFont, "Inter", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    .canvas {{
      position: relative;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background:
        radial-gradient(circle at 14% 48%, rgba(0, 255, 202, .72), rgba(0, 255, 202, .18) 12%, rgba(0, 255, 202, 0) 28%),
        radial-gradient(circle at 88% 48%, rgba(0, 102, 255, .76), rgba(0, 102, 255, .18) 14%, rgba(0, 102, 255, 0) 31%),
        radial-gradient(circle at 50% 52%, rgba(35, 45, 56, .72), rgba(35, 45, 56, 0) 40%),
        linear-gradient(180deg, #0e1217 0%, #080d12 100%);
    }}
    .grain {{
      position: absolute;
      inset: 0;
      opacity: .16;
      background-image:
        linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px),
        linear-gradient(0deg, rgba(255,255,255,.025) 1px, transparent 1px);
      background-size: 34px 34px;
      mask-image: radial-gradient(circle at 50% 50%, #000, transparent 74%);
    }}
    .caption {{
      position: absolute;
      left: 0;
      width: 100%;
      text-align: center;
      color: #f8f8f8;
      font-size: 56px;
      line-height: .98;
      font-weight: 900;
      letter-spacing: -0.055em;
      text-shadow: 0 4px 0 rgba(255,255,255,.08), 0 20px 46px rgba(0,0,0,.48);
      z-index: 5;
    }}
    .caption.top {{
      display: {top_caption};
      top: 126px;
    }}
    .caption.bottom {{
      display: {bottom_caption};
      bottom: 118px;
    }}
    .icon-hero {{
      display: {icon_display};
      position: absolute;
      left: 50%;
      top: 765px;
      width: 430px;
      height: 430px;
      transform: translateX(-50%);
      border-radius: 110px;
      background:
        radial-gradient(circle at 50% 46%, rgba(255,255,255,.16), transparent 28%),
        linear-gradient(145deg, #173b55, #0d1d2c 52%, #0b141e);
      box-shadow:
        0 60px 130px rgba(0,0,0,.54),
        0 0 120px rgba(0, 239, 200, .24),
        inset 0 0 0 1px rgba(255,255,255,.08);
      z-index: 4;
    }}
    .icon-core {{
      position: absolute;
      left: 50%;
      top: 50%;
      width: 252px;
      height: 252px;
      transform: translate(-50%, -50%);
      border-radius: 70px;
      background: linear-gradient(145deg, #21d99b, #00a978);
      box-shadow:
        0 38px 70px rgba(0,0,0,.34),
        inset 0 8px 20px rgba(255,255,255,.24);
    }}
    .swirl {{
      position: absolute;
      left: 50%;
      top: 50%;
      width: 128px;
      height: 128px;
      transform: translate(-50%, -50%);
      border-radius: 50%;
      border: 18px solid rgba(255,255,255,.92);
      border-left-color: transparent;
      border-bottom-color: transparent;
    }}
    .swirl::before,
    .swirl::after {{
      content: "";
      position: absolute;
      width: 72px;
      height: 72px;
      border-radius: 50%;
      border: 16px solid rgba(255,255,255,.92);
      border-right-color: transparent;
      border-top-color: transparent;
    }}
    .swirl::before {{
      left: -26px;
      top: 34px;
    }}
    .swirl::after {{
      right: -26px;
      top: 20px;
      transform: rotate(180deg);
    }}
    .phone {{
      display: {phone_display};
      position: absolute;
      left: 50%;
      top: {variant["phone_top"]};
      width: {variant["phone_width"]};
      height: {variant["phone_height"]};
      transform: translateX(-50%) rotate({variant["tilt"]});
      border-radius: 58px;
      background: #070a0d;
      box-shadow:
        0 54px 110px rgba(0,0,0,.60),
        0 0 82px rgba(0, 169, 255, .18),
        inset 0 0 0 12px #0a0d10,
        inset 0 0 0 15px rgba(255,255,255,.045);
      overflow: hidden;
      z-index: 4;
    }}
    .screen {{
      position: absolute;
      left: 24px;
      top: 36px;
      right: 24px;
      bottom: 36px;
      border-radius: 42px;
      overflow: hidden;
      background: {variant["screen_bg"]};
    }}
    .screen img {{
      display: block;
      width: 100%;
      height: 100%;
      object-fit: cover;
      object-position: center top;
      filter: contrast(1.04) saturate(1.02);
    }}
    .top-shine {{
      position: absolute;
      left: 50%;
      top: 18px;
      width: 100px;
      height: 16px;
      transform: translateX(-50%);
      border-radius: 999px;
      background: #050607;
      z-index: 6;
    }}
  </style>
</head>
<body>
  <main class="canvas">
    <div class="grain"></div>
    <section class="caption top">{top_headline_html}</section>
    <section class="icon-hero">
      <div class="icon-core"><div class="swirl"></div></div>
    </section>
    <section class="phone">
      <div class="top-shine"></div>
      <div class="screen"><img src="{img_src}" alt=""></div>
    </section>
    <section class="caption bottom">{bottom_headline_html}</section>
  </main>
</body>
</html>
"""


def render_html(
    *,
    screenshot_path: Path,
    template: dict,
    copy: str,
    width: int,
    height: int,
    index: int = 1,
) -> str:
    layout = str(template.get("layout") or "clean-discover-phone")
    if layout == "clean-discover-phone":
        return render_clean_discover_html(
            screenshot_path=screenshot_path,
            template=template,
            copy=copy,
            width=width,
            height=height,
        )
    if layout == "purple-live-phone":
        return render_purple_live_html(
            screenshot_path=screenshot_path,
            template=template,
            copy=copy,
            width=width,
            height=height,
        )
    if layout == "spotify-pink-series":
        return render_spotify_pink_series_html(
            screenshot_path=screenshot_path,
            template=template,
            copy=copy,
            width=width,
            height=height,
            index=index,
        )
    if layout == "element-dark-glow-series":
        return render_element_dark_glow_series_html(
            screenshot_path=screenshot_path,
            template=template,
            copy=copy,
            width=width,
            height=height,
            index=index,
        )
    raise RuntimeError(f"Unsupported HTML layout: {layout}")


def find_chrome() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    resolved = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if resolved:
        return Path(resolved)
    raise RuntimeError("No local Chrome/Chromium executable found for HTML screenshot rendering")


def screenshot_html(chrome: Path, html_path: Path, output_path: Path, width: int, height: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="market-html-chrome-") as user_data_dir:
        cmd = [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-sync",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--allow-file-access-from-files",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=1000",
            f"--user-data-dir={user_data_dir}",
            f"--window-size={width},{height}",
            f"--screenshot={output_path}",
            html_path.as_uri(),
        ]
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired as exc:
            if output_path.exists():
                return
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            raise RuntimeError(f"Chrome screenshot timed out: {(stderr or stdout).strip()}") from exc
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    if not output_path.exists():
        raise RuntimeError(f"Chrome did not create screenshot: {output_path}")


def validate_png_size(path: Path, width: int, height: int) -> None:
    if Image is None:
        return
    with Image.open(path) as image:
        if image.size != (width, height):
            raise RuntimeError(f"Output size mismatch: {path} is {image.size[0]}x{image.size[1]}, expected {width}x{height}")


def render_html_file(
    *,
    image_path: Path,
    template: dict,
    copy: str,
    html_path: Path,
    width: int,
    height: int,
    index: int,
    override: dict,
) -> None:
    html_path.parent.mkdir(parents=True, exist_ok=True)
    effective_copy = copy_with_override(template, copy, index, override)
    rendered = render_html(
        screenshot_path=image_path,
        template=template,
        copy=effective_copy,
        width=width,
        height=height,
        index=index,
    )
    rendered = inject_override_css(rendered, override)
    html_path.write_text(rendered, encoding="utf-8")


def selected_templates(pack: dict, selected_template_name: str | None = None) -> list[dict]:
    templates = pack["templates"]
    if not selected_template_name:
        return templates
    selected = [item for item in templates if str(item.get("name")) == selected_template_name]
    if not selected:
        raise RuntimeError(f"Unknown template: {selected_template_name}")
    return selected


def build_jobs(
    pack: dict,
    screenshots: list[Path],
    output_dir: Path,
    copy_map: dict[str, str],
    selected_template_name: str | None = None,
) -> list[dict]:
    templates = selected_templates(pack, selected_template_name)
    width, height = canvas_size(pack)
    html_dir = output_dir / "_html"
    jobs: list[dict] = []
    total = len(screenshots)
    for zero_index, image_path in enumerate(screenshots):
        index = zero_index + 1
        template = templates[zero_index % len(templates)]
        copy = copy_for_image(copy_map, image_path, index)
        html_path = html_dir / f"{index:02d}.html"
        output_path = output_dir / f"{index:02d}.png"
        jobs.append(
            {
                "index": f"{index:02d}",
                "template": str(template["name"]),
                "input_image": str(image_path),
                "html": str(html_path),
                "output": str(output_path),
                "canvas": {"width": width, "height": height},
                "copy": copy,
            }
        )
    return jobs


def write_preview_index(output_dir: Path, jobs: list[dict], width: int, height: int) -> Path:
    scale = 0.235
    preview_w = int(width * scale)
    preview_h = int(height * scale)
    cards: list[str] = []
    for job in jobs:
        rel_html = Path(job["html"]).relative_to(output_dir).as_posix()
        output_rel = Path(job["output"]).relative_to(output_dir).as_posix()
        title = html.escape(f"{job['index']} · {job['template']}")
        input_name = html.escape(Path(job["input_image"]).name)
        cards.append(
            f"""
      <article class="card">
        <div class="meta">
          <strong>{title}</strong>
          <span>{input_name}</span>
        </div>
        <a class="preview" href="{rel_html}" target="_blank" style="width:{preview_w}px;height:{preview_h}px">
          <iframe src="{rel_html}" title="{title}" style="width:{width}px;height:{height}px;transform:scale({scale});"></iframe>
        </a>
        <div class="links">
          <a href="{rel_html}" target="_blank">Open HTML</a>
          <a href="edit/{job['index']}">Edit</a>
          <span>{output_rel}</span>
        </div>
      </article>"""
        )
    index = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Market Preview</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 32px;
      min-height: 100vh;
      background: #eef0f4;
      color: #15171c;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    header {{
      max-width: 1180px;
      margin: 0 auto 28px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
      line-height: 1.2;
    }}
    p {{
      margin: 0;
      color: #626977;
    }}
    .grid {{
      max-width: 1180px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 22px;
    }}
    .card {{
      border-radius: 24px;
      background: rgba(255,255,255,.9);
      box-shadow: 0 18px 50px rgba(25,31,45,.10);
      padding: 18px;
    }}
    .meta {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      margin-bottom: 14px;
      font-size: 14px;
    }}
    .meta span {{
      color: #768092;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .preview {{
      display: block;
      position: relative;
      overflow: hidden;
      margin: 0 auto;
      border-radius: 18px;
      background: #fff;
      box-shadow: 0 12px 28px rgba(18, 22, 32, .16);
    }}
    iframe {{
      display: block;
      border: 0;
      transform-origin: top left;
      pointer-events: none;
    }}
    .links {{
      margin-top: 14px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: #768092;
      font-size: 13px;
    }}
    a {{
      color: #2868f0;
      text-decoration: none;
    }}
    .confirm-bar {{
      position: sticky;
      bottom: 0;
      max-width: 1180px;
      margin: 28px auto 0;
      padding: 16px;
      border-radius: 22px;
      background: rgba(255,255,255,.94);
      box-shadow: 0 -10px 40px rgba(25,31,45,.12);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      backdrop-filter: blur(12px);
    }}
    button {{
      border: 0;
      border-radius: 14px;
      background: #111318;
      color: #fff;
      font: inherit;
      font-weight: 800;
      padding: 13px 18px;
      cursor: pointer;
    }}
    #status {{
      color: #626977;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>Market Preview</h1>
    <p>先检查 HTML 预览，可点 Edit 调整。满意后点击底部确认按钮，系统会直接截图导出 PNG。</p>
  </header>
  <main class="grid">
{''.join(cards)}
  </main>
  <section class="confirm-bar">
    <div>
      <strong>确认当前预览</strong>
      <div id="status">点击后会按当前 HTML 和 overrides.json 导出 PNG。</div>
    </div>
    <button id="confirm">确认生成截图</button>
  </section>
  <script>
    const button = document.querySelector('#confirm');
    const status = document.querySelector('#status');
    button.addEventListener('click', async () => {{
      button.disabled = true;
      status.textContent = '正在截图导出，请稍等...';
      try {{
        const res = await fetch('/api/confirm-generate', {{ method: 'POST' }});
        const text = await res.text();
        if (!res.ok) throw new Error(text);
        const data = JSON.parse(text);
        const failed = data.failed_jobs || [];
        status.textContent = failed.length
          ? '部分失败：' + failed.map(item => item.index).join(', ')
          : '已生成 ' + (data.outputs || []).length + ' 张 PNG。';
      }} catch (error) {{
        status.textContent = '生成失败：' + error.message;
      }} finally {{
        button.disabled = false;
      }}
    }});
  </script>
</body>
</html>
"""
    index_path = output_dir / "index.html"
    index_path.write_text(index, encoding="utf-8")
    return index_path


def job_by_index(jobs: list[dict], index_key: str) -> dict | None:
    for job in jobs:
        if job.get("index") == index_key:
            return job
    return None


def template_for_index(pack: dict, index: int, selected_template_name: str | None = None) -> dict:
    templates = selected_templates(pack, selected_template_name)
    return templates[(index - 1) % len(templates)]


def template_for_state_index(state: dict, index: int) -> dict:
    return template_for_index(state["pack"], index, state.get("selected_template_name"))


def screenshot_for_index(screenshots: list[Path], index: int) -> Path:
    return screenshots[index - 1]


def render_one_preview_html(state: dict, index: int) -> None:
    template = template_for_state_index(state, index)
    image_path = screenshot_for_index(state["screenshots"], index)
    copy = copy_for_image(state["copy_map"], image_path, index)
    html_path = state["output_dir"] / "_html" / f"{index:02d}.html"
    render_html_file(
        image_path=image_path,
        template=template,
        copy=copy,
        html_path=html_path,
        width=state["width"],
        height=state["height"],
        index=index,
        override=override_for_index(state["overrides"], index),
    )


def render_edit_page(state: dict, index_key: str) -> str:
    job = job_by_index(state["jobs"], index_key)
    if not job:
        raise FileNotFoundError(index_key)
    index = int(index_key)
    template = template_for_state_index(state, index)
    image_path = screenshot_for_index(state["screenshots"], index)
    copy = copy_for_image(state["copy_map"], image_path, index)
    default_top, default_bottom = default_titles(template, copy, index)
    override = sanitize_override(override_for_index(state["overrides"], index))
    iframe_src = f"/_html/{index_key}.html?v={int(time.time())}"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Edit {index_key}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: #eef0f4;
      color: #15171c;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    .page {{
      display: grid;
      grid-template-columns: 420px 1fr;
      gap: 24px;
      padding: 24px;
      min-height: 100vh;
    }}
    aside {{
      background: rgba(255,255,255,.94);
      border-radius: 24px;
      box-shadow: 0 18px 50px rgba(25,31,45,.10);
      padding: 22px;
      height: fit-content;
      position: sticky;
      top: 24px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 24px;
    }}
    .sub {{
      margin: 0 0 20px;
      color: #697386;
      font-size: 13px;
      line-height: 1.5;
    }}
    label {{
      display: block;
      margin: 16px 0 7px;
      font-size: 13px;
      font-weight: 700;
    }}
    input {{
      width: 100%;
      height: 40px;
      border: 1px solid #d8deea;
      border-radius: 12px;
      padding: 0 12px;
      font: inherit;
      background: #fff;
    }}
    input[type="color"] {{
      padding: 4px;
    }}
    .row {{
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 10px;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      margin-top: 20px;
    }}
    button, a.button {{
      border: 0;
      border-radius: 12px;
      background: #15171c;
      color: #fff;
      font: inherit;
      font-weight: 700;
      padding: 11px 14px;
      cursor: pointer;
      text-decoration: none;
      text-align: center;
    }}
    button.secondary, a.button.secondary {{
      background: #e8ecf3;
      color: #15171c;
    }}
    .preview-wrap {{
      display: grid;
      place-items: start center;
      overflow: auto;
      padding: 12px 0 40px;
    }}
    iframe {{
      width: {state["width"]}px;
      height: {state["height"]}px;
      border: 0;
      transform: scale(.42);
      transform-origin: top center;
      border-radius: 18px;
      box-shadow: 0 24px 70px rgba(25,31,45,.22);
    }}
    @media (max-width: 980px) {{
      .page {{ grid-template-columns: 1fr; }}
      aside {{ position: static; }}
      iframe {{ transform: scale(.3); }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <aside>
      <h1>Edit {html.escape(index_key)}</h1>
      <p class="sub">只允许修改标题文字、截图位置/缩放、背景色。其他模板结构保持锁定。</p>
      <form id="editor">
        <label>标题上 / 小标题</label>
        <input name="title_top" value="{html.escape(override.get("title_top") or "")}" placeholder="{html.escape(default_top)}">
        <label>标题下 / 大标题</label>
        <input name="title_bottom" value="{html.escape(override.get("title_bottom") or "")}" placeholder="{html.escape(default_bottom)}">
        <label>背景色</label>
        <input name="background_color" value="{html.escape(override.get("background_color") or "")}" placeholder="#000000">
        <div class="row">
          <div>
            <label>截图 X</label>
            <input name="screenshot_x" type="number" step="1" value="{override.get("screenshot_x", 0)}">
          </div>
          <div>
            <label>截图 Y</label>
            <input name="screenshot_y" type="number" step="1" value="{override.get("screenshot_y", 0)}">
          </div>
          <div>
            <label>缩放</label>
            <input name="screenshot_scale" type="number" step="0.01" min="0.5" max="2" value="{override.get("screenshot_scale", 1)}">
          </div>
        </div>
        <div class="actions">
          <button type="submit">保存并刷新预览</button>
          <button class="secondary" type="button" id="reset">重置</button>
          <a class="button secondary" href="/">返回列表</a>
        </div>
      </form>
    </aside>
    <section class="preview-wrap">
      <iframe id="preview" src="{iframe_src}"></iframe>
    </section>
  </main>
  <script>
    const form = document.querySelector('#editor');
    const preview = document.querySelector('#preview');
    const endpoint = '/api/overrides/{index_key}';
    function values(reset = false) {{
      if (reset) return {{}};
      const data = new FormData(form);
      return {{
        title_top: data.get('title_top') || '',
        title_bottom: data.get('title_bottom') || '',
        background_color: data.get('background_color') || '',
        screenshot_x: Number(data.get('screenshot_x') || 0),
        screenshot_y: Number(data.get('screenshot_y') || 0),
        screenshot_scale: Number(data.get('screenshot_scale') || 1)
      }};
    }}
    async function save(payload) {{
      const res = await fetch(endpoint, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }});
      if (!res.ok) {{
        alert(await res.text());
        return;
      }}
      preview.src = '/_html/{index_key}.html?v=' + Date.now();
    }}
    form.addEventListener('submit', (event) => {{
      event.preventDefault();
      save(values(false));
    }});
    document.querySelector('#reset').addEventListener('click', () => {{
      form.reset();
      save(values(true));
    }});
  </script>
</body>
</html>
"""


def render_template_selection_page(state: dict) -> str:
    templates = state["pack"]["templates"]
    cards: list[str] = []
    for item in templates:
        name = str(item.get("name") or "")
        layout = str(item.get("layout") or "")
        prompt = str(item.get("prompt") or "")
        cards.append(
            f"""
      <article class="card">
        <h2>{html.escape(name)}</h2>
        <p class="layout">{html.escape(layout)}</p>
        <p>{html.escape(prompt[:260])}</p>
        <a class="button" href="/select/{urllib.parse.quote(name)}">选择这套模板</a>
      </article>"""
        )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Choose Template</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      padding: 36px;
      background:
        radial-gradient(circle at 12% 18%, rgba(90,104,255,.18), transparent 28%),
        radial-gradient(circle at 88% 8%, rgba(255,78,160,.14), transparent 24%),
        #eef0f4;
      color: #15171c;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    header {{
      max-width: 1120px;
      margin: 0 auto 28px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 34px;
      letter-spacing: -.03em;
    }}
    header p {{
      margin: 0;
      color: #626977;
    }}
    .grid {{
      max-width: 1120px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 18px;
    }}
    .card {{
      min-height: 260px;
      padding: 22px;
      border-radius: 26px;
      background: rgba(255,255,255,.92);
      box-shadow: 0 18px 50px rgba(25,31,45,.10);
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    h2 {{
      margin: 0;
      font-size: 22px;
      line-height: 1.15;
    }}
    p {{
      margin: 0;
      color: #626977;
      line-height: 1.55;
    }}
    .layout {{
      color: #2868f0;
      font-weight: 800;
    }}
    .button {{
      margin-top: auto;
      display: inline-block;
      width: fit-content;
      border-radius: 14px;
      background: #111318;
      color: white;
      padding: 12px 16px;
      text-decoration: none;
      font-weight: 800;
    }}
  </style>
</head>
<body>
  <header>
    <h1>选择市场图模板</h1>
    <p>先选一套模板，系统再生成 HTML 预览。预览满意后点击底部确认按钮导出 PNG。</p>
  </header>
  <main class="grid">
{''.join(cards)}
  </main>
</body>
</html>
"""


def refresh_jobs_for_selected_template(state: dict) -> None:
    state["jobs"] = build_jobs(
        state["pack"],
        state["screenshots"],
        state["output_dir"],
        state["copy_map"],
        state.get("selected_template_name"),
    )


def render_all_preview_html(state: dict) -> None:
    html_dir = state["output_dir"] / "_html"
    html_dir.mkdir(parents=True, exist_ok=True)
    refresh_jobs_for_selected_template(state)
    for zero_index, image_path in enumerate(state["screenshots"]):
        index = zero_index + 1
        render_one_preview_html(state, index)
    write_preview_index(state["output_dir"], state["jobs"], state["width"], state["height"])


def generate_png_outputs(state: dict) -> dict:
    chrome = find_chrome()
    render_all_preview_html(state)
    outputs: list[dict] = []
    failed: list[dict] = []
    for job in state["jobs"]:
        html_path = Path(job["html"])
        output_path = Path(job["output"])
        try:
            screenshot_html(chrome, html_path, output_path, state["width"], state["height"])
            validate_png_size(output_path, state["width"], state["height"])
            outputs.append(
                {
                    "index": job["index"],
                    "template": job["template"],
                    "input_image": job["input_image"],
                    "html": str(html_path),
                    "output": str(output_path),
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "index": job["index"],
                    "template": job.get("template"),
                    "input_image": job.get("input_image"),
                    "error": str(exc),
                }
            )
    return {
        "mode": "html_prompt_pack_generation",
        "pack_name": state["pack"].get("pack_name") or "prompt-pack",
        "template": state.get("selected_template_name"),
        "renderer": str(chrome),
        "screenshots_dir": str(state["screenshots_dir"]),
        "output_dir": str(state["output_dir"]),
        "canvas": {"width": state["width"], "height": state["height"]},
        "outputs": outputs,
        "failed_jobs": failed,
    }


class PreviewHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, state: dict, directory: str, **kwargs: object) -> None:
        self.state = state
        super().__init__(*args, directory=directory, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        return

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/index.html"} and not self.state.get("selected_template_name"):
            self.send_text(render_template_selection_page(self.state))
            return
        select_match = re.fullmatch(r"/select/(.+)", parsed.path)
        if select_match:
            try:
                name = urllib.parse.unquote(select_match.group(1))
                self.state["selected_template_name"] = name
                self.state["overrides"] = load_overrides(self.state["output_dir"])
                render_all_preview_html(self.state)
                self.send_response(302)
                self.send_header("Location", "/preview")
                self.end_headers()
            except Exception as exc:
                self.send_text(str(exc), status=500, content_type="text/plain; charset=utf-8")
            return
        if parsed.path == "/preview":
            if not self.state.get("selected_template_name"):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            try:
                write_preview_index(self.state["output_dir"], self.state["jobs"], self.state["width"], self.state["height"])
                self.send_text((self.state["output_dir"] / "index.html").read_text(encoding="utf-8"))
            except Exception as exc:
                self.send_text(str(exc), status=500, content_type="text/plain; charset=utf-8")
            return
        match = re.fullmatch(r"/edit/(\d+)", parsed.path)
        if match:
            if not self.state.get("selected_template_name"):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            try:
                self.send_text(render_edit_page(self.state, f"{int(match.group(1)):02d}"))
            except FileNotFoundError:
                self.send_text("Not found", status=404, content_type="text/plain; charset=utf-8")
            except Exception as exc:
                self.send_text(str(exc), status=500, content_type="text/plain; charset=utf-8")
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/confirm-generate":
            if not self.state.get("selected_template_name"):
                self.send_text("Choose a template before generating screenshots", status=409, content_type="text/plain; charset=utf-8")
                return
            try:
                self.state["overrides"] = load_overrides(self.state["output_dir"])
                self.send_json(generate_png_outputs(self.state))
            except Exception as exc:
                self.send_text(str(exc), status=500, content_type="text/plain; charset=utf-8")
            return
        match = re.fullmatch(r"/api/overrides/(\d+)", parsed.path)
        if not match:
            self.send_text("Not found", status=404, content_type="text/plain; charset=utf-8")
            return
        index = int(match.group(1))
        index_key = f"{index:02d}"
        if not job_by_index(self.state["jobs"], index_key):
            self.send_text("Unknown image index", status=404, content_type="text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw or "{}")
            if not isinstance(payload, dict):
                raise RuntimeError("Payload must be a JSON object")
            cleaned = sanitize_override(payload)
            if payload:
                self.state["overrides"][index_key] = cleaned
            else:
                self.state["overrides"].pop(index_key, None)
            save_overrides(self.state["output_dir"], self.state["overrides"])
            render_one_preview_html(self.state, index)
            write_preview_index(self.state["output_dir"], self.state["jobs"], self.state["width"], self.state["height"])
            self.send_json({"ok": True, "index": index_key, "override": self.state["overrides"].get(index_key, {})})
        except Exception as exc:
            self.send_text(str(exc), status=500, content_type="text/plain; charset=utf-8")


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def serve_preview(state: dict, host: str, port: int) -> None:
    output_dir = state["output_dir"]
    handler = functools.partial(PreviewHTTPRequestHandler, state=state, directory=str(output_dir))
    with ReusableTCPServer((host, port), handler) as httpd:
        url = f"http://{host}:{port}/"
        print(
            json.dumps(
                {
                    "mode": "html_prompt_pack_preview_server",
                    "url": url,
                    "preview_index": str(output_dir / "index.html"),
                    "serving_dir": str(output_dir),
                    "stop": "Press Ctrl+C to stop the preview server.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nPreview server stopped.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate app market images from local HTML prompt templates.")
    parser.add_argument("--list-packs", action="store_true", help="List built-in prompt packs and stop.")
    parser.add_argument("--prompt-pack-dir", help="Local prompt pack directory. Default: built-in default-googleplay-prompt-pack.")
    parser.add_argument("--screenshots-dir", help="Required directory containing numbered screenshots such as 01.png or 01.jpg.")
    parser.add_argument("--output-dir", help="Default: <screenshots-dir>/newImage")
    parser.add_argument("--copy-json", help='Optional JSON object, for example {"default":"发现|多元兴趣方式"}')
    parser.add_argument("--copy-file", help="Optional JSON file for copy values")
    parser.add_argument("--dry-run", action="store_true", help="Render jobs without writing PNG screenshots.")
    parser.add_argument("--serve-preview", action="store_true", help="Render HTML files and start a local preview web server.")
    parser.add_argument("--host", default=DEFAULT_PREVIEW_HOST, help=f"Preview server host. Default: {DEFAULT_PREVIEW_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PREVIEW_PORT, help=f"Preview server port. Default: {DEFAULT_PREVIEW_PORT}")
    parser.add_argument("--generate", action="store_true", help="Render HTML and screenshot PNG files locally.")
    args = parser.parse_args()

    if args.list_packs:
        print(json.dumps({"mode": "list_prompt_packs", "prompt_packs": list_prompt_packs()}, ensure_ascii=False, indent=2))
        return 0

    if not args.screenshots_dir:
        raise RuntimeError("--screenshots-dir is required")
    if not args.dry_run and not args.serve_preview and not args.generate:
        raise RuntimeError("Use --dry-run, --serve-preview, or --generate")

    pack_dir = Path(args.prompt_pack_dir).expanduser().resolve() if args.prompt_pack_dir else DEFAULT_PACK_DIR.resolve()
    screenshots_dir = Path(args.screenshots_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else screenshots_dir / "newImage"
    output_dir.mkdir(parents=True, exist_ok=True)

    pack = load_pack(pack_dir)
    screenshots = sorted_input_images(screenshots_dir)
    if not screenshots:
        raise RuntimeError("No numbered screenshots found. Use names such as 01.png, 02.jpg, or 03.webp.")
    copy_map = load_copy_map(args.copy_json, args.copy_file)
    width, height = canvas_size(pack)
    jobs = build_jobs(pack, screenshots, output_dir, copy_map)
    overrides = load_overrides(output_dir)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "html_prompt_pack_dry_run",
                    "pack_name": pack.get("pack_name") or pack_dir.name,
                    "pack_dir": str(pack_dir),
                    "screenshots_dir": str(screenshots_dir),
                "output_dir": str(output_dir),
                "overrides": str(output_dir / OVERRIDES_FILENAME),
                "canvas": {"width": width, "height": height},
                "jobs": jobs,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.serve_preview:
        state = {
            "pack": pack,
            "screenshots_dir": screenshots_dir,
            "screenshots": screenshots,
            "copy_map": copy_map,
            "output_dir": output_dir,
            "width": width,
            "height": height,
            "jobs": [],
            "overrides": overrides,
            "selected_template_name": None,
        }
        serve_preview(state, args.host, args.port)
        return 0

    chrome = find_chrome()
    html_dir = output_dir / "_html"
    html_dir.mkdir(parents=True, exist_ok=True)
    templates = pack["templates"]
    outputs: list[dict] = []
    failed: list[dict] = []
    for zero_index, image_path in enumerate(screenshots):
        index = zero_index + 1
        template = templates[zero_index % len(templates)]
        copy = copy_for_image(copy_map, image_path, index)
        html_path = html_dir / f"{index:02d}.html"
        output_path = output_dir / f"{index:02d}.png"
        try:
            render_html_file(
                image_path=image_path,
                template=template,
                copy=copy,
                html_path=html_path,
                width=width,
                height=height,
                index=index,
                override=override_for_index(overrides, index),
            )
            screenshot_html(chrome, html_path, output_path, width, height)
            validate_png_size(output_path, width, height)
            outputs.append(
                {
                    "index": f"{index:02d}",
                    "template": template["name"],
                    "input_image": str(image_path),
                    "html": str(html_path),
                    "output": str(output_path),
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "index": f"{index:02d}",
                    "template": template.get("name"),
                    "input_image": str(image_path),
                    "error": str(exc),
                }
            )

    print(
        json.dumps(
            {
                "mode": "html_prompt_pack_generation",
                "pack_name": pack.get("pack_name") or pack_dir.name,
                "pack_dir": str(pack_dir),
                "renderer": str(chrome),
                "screenshots_dir": str(screenshots_dir),
                "output_dir": str(output_dir),
                "canvas": {"width": width, "height": height},
                "outputs": outputs,
                "failed_jobs": failed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
