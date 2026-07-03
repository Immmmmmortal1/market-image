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


def iter_catalog_templates() -> list[dict]:
    catalog: list[dict] = []
    if not PROMPT_PACKS_DIR.exists():
        return catalog
    for pack_dir in sorted(PROMPT_PACKS_DIR.iterdir()):
        if not pack_dir.is_dir() or not (pack_dir / "pack.json").exists():
            continue
        try:
            pack = load_pack(pack_dir)
        except Exception:
            continue
        pack_name = str(pack.get("pack_name") or pack_dir.name)
        for item in pack.get("templates") or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            catalog.append(
                {
                    "pack_name": pack_name,
                    "pack_dir": str(pack_dir.resolve()),
                    "template_name": str(item["name"]),
                    "layout": str(item.get("layout") or ""),
                    "prompt": str(item.get("prompt") or ""),
                }
            )
    return catalog


def resolve_template_selection(template_name: str) -> tuple[dict, str]:
    matches: list[tuple[dict, str]] = []
    if not PROMPT_PACKS_DIR.exists():
        raise RuntimeError(f"Unknown template: {template_name}")
    for pack_dir in sorted(PROMPT_PACKS_DIR.iterdir()):
        if not pack_dir.is_dir() or not (pack_dir / "pack.json").exists():
            continue
        pack = load_pack(pack_dir)
        for item in pack.get("templates") or []:
            if isinstance(item, dict) and str(item.get("name") or "") == template_name:
                matches.append((pack, template_name))
    if not matches:
        raise RuntimeError(f"Unknown template: {template_name}")
    if len(matches) > 1:
        pack_names = ", ".join(sorted({str(p.get("pack_name") or "") for p, _ in matches}))
        raise RuntimeError(f"Template name {template_name!r} is ambiguous across packs: {pack_names}")
    return matches[0]


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
        variant = clean_discover_defaults(template, index)
        return split_copy(
            copy,
            variant["headline_small"],
            variant["headline_large"].replace("\\n", "\n"),
        )
    if layout == "purple-live-phone":
        return split_copy(
            copy,
            str(template.get("headline_small") or "发现新直播"),
            str(template.get("headline_large") or "剪辑与故事"),
        )
    if layout == "douyin-deal-card":
        return split_copy(
            copy,
            str(template.get("headline_small") or "额外加料！"),
            str(template.get("headline_large") or "同款更低价"),
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


def _clamp_byte(value: float) -> int:
    return max(0, min(255, int(round(value))))


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _mix_rgb(a: tuple[int, int, int], b: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    ratio = max(0.0, min(1.0, ratio))
    inv = 1.0 - ratio
    return (
        _clamp_byte(a[0] * inv + b[0] * ratio),
        _clamp_byte(a[1] * inv + b[1] * ratio),
        _clamp_byte(a[2] * inv + b[2] * ratio),
    )


def extract_accent_palette(screenshot_path: Path) -> dict[str, str]:
    defaults = {
        "accent": "#5566ff",
        "accent_soft": "rgba(85, 102, 255, 0.14)",
        "accent_glow": "rgba(85, 102, 255, 0.22)",
        "bg_top": "#fbfcfe",
        "bg_bottom": "#eef1f7",
        "ink": "#12141a",
        "ink_muted": "#5d6472",
    }
    if Image is None:
        return defaults
    try:
        with Image.open(screenshot_path) as img:
            img = img.convert("RGB")
            img = img.resize((56, 56))
            pixels = list(img.getdata())
    except Exception:
        return defaults

    scored: list[tuple[float, tuple[int, int, int]]] = []
    for r, g, b in pixels:
        rn, gn, bn = r / 255.0, g / 255.0, b / 255.0
        max_c = max(rn, gn, bn)
        min_c = min(rn, gn, bn)
        delta = max_c - min_c
        lightness = (max_c + min_c) / 2.0
        if delta < 0.08 or lightness < 0.12 or lightness > 0.92:
            continue
        saturation = delta / max(0.001, 1.0 - abs(2.0 * lightness - 1.0))
        scored.append((saturation * (1.0 - abs(lightness - 0.52)), (r, g, b)))
    if not scored:
        return defaults

    scored.sort(key=lambda item: item[0], reverse=True)
    top = [rgb for _, rgb in scored[: max(8, len(scored) // 5)]]
    accent = (
        sum(item[0] for item in top) // len(top),
        sum(item[1] for item in top) // len(top),
        sum(item[2] for item in top) // len(top),
    )
    bg_top = _mix_rgb(accent, (255, 255, 255), 0.92)
    bg_bottom = _mix_rgb(accent, (238, 241, 247), 0.72)
    return {
        "accent": _rgb_to_hex(*accent),
        "accent_soft": f"rgba({accent[0]}, {accent[1]}, {accent[2]}, 0.14)",
        "accent_glow": f"rgba({accent[0]}, {accent[1]}, {accent[2]}, 0.24)",
        "bg_top": _rgb_to_hex(*bg_top),
        "bg_bottom": _rgb_to_hex(*bg_bottom),
        "ink": "#12141a",
        "ink_muted": _rgb_to_hex(*_mix_rgb(accent, (93, 100, 114), 0.78)),
    }


def clean_discover_defaults(template: dict, index: int) -> dict:
    variants = template.get("variants")
    selected: dict = {}
    if isinstance(variants, list) and variants:
        selected = variants[min(index - 1, len(variants) - 1)]
        if not isinstance(selected, dict):
            selected = {}
    return {
        "headline_small": str(selected.get("headline_small") or template.get("headline_small") or "更快上手"),
        "headline_large": str(selected.get("headline_large") or template.get("headline_large") or "核心功能\n一目了然"),
        "phone_tilt": str(selected.get("phone_tilt") or "-2.5deg"),
        "phone_top": str(selected.get("phone_top") or "462px"),
        "phone_shift_x": str(selected.get("phone_shift_x") or "36px"),
        "copy_left": str(selected.get("copy_left") or "84px"),
        "copy_top": str(selected.get("copy_top") or "128px"),
        "badge": str(selected.get("badge") or ""),
    }


def render_clean_discover_html(
    *,
    screenshot_path: Path,
    template: dict,
    copy: str,
    width: int,
    height: int,
    index: int = 1,
) -> str:
    variant = clean_discover_defaults(template, index)
    small, large = split_copy(
        copy,
        variant["headline_small"],
        variant["headline_large"].replace("\\n", "\n"),
    )
    palette = extract_accent_palette(screenshot_path)
    img_src = image_data_uri(screenshot_path)
    small_html = html.escape(small)
    large_lines = [html.escape(part) for part in large.splitlines() if part.strip()]
    if not large_lines:
        large_lines = [html.escape(large)]
    large_html = "<br>".join(large_lines)
    badge_html = html.escape(variant["badge"]) if variant["badge"] else ""
    badge_block = (
        f'<span class="badge">{badge_html}</span>' if badge_html else ""
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width={width}, initial-scale=1">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,500;0,9..40,700;0,9..40,800;1,9..40,500&family=Noto+Sans+SC:wght@500;700;900&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background: {palette["bg_top"]};
      font-family: "DM Sans", "Noto Sans SC", -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
    }}
    .canvas {{
      position: relative;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      background:
        radial-gradient(circle at 18% 14%, {palette["accent_glow"]}, transparent 28%),
        radial-gradient(circle at 88% 18%, rgba(255,255,255,.72), transparent 24%),
        radial-gradient(circle at 72% 82%, {palette["accent_soft"]}, transparent 34%),
        linear-gradient(165deg, {palette["bg_top"]} 0%, {palette["bg_bottom"]} 100%);
    }}
    .grain {{
      position: absolute;
      inset: 0;
      opacity: .08;
      background-image:
        linear-gradient(90deg, rgba(255,255,255,.05) 1px, transparent 1px),
        linear-gradient(0deg, rgba(0,0,0,.025) 1px, transparent 1px);
      background-size: 28px 28px;
      mask-image: radial-gradient(circle at 50% 42%, #000, transparent 78%);
      pointer-events: none;
    }}
    .accent-line {{
      position: absolute;
      left: {variant["copy_left"]};
      top: calc({variant["copy_top"]} + 8px);
      width: 56px;
      height: 6px;
      border-radius: 999px;
      background: linear-gradient(90deg, {palette["accent"]}, rgba(255,255,255,0));
      z-index: 3;
    }}
    .copy {{
      position: absolute;
      top: {variant["copy_top"]};
      left: {variant["copy_left"]};
      width: calc(100% - 120px);
      text-align: left;
      color: {palette["ink"]};
      z-index: 3;
    }}
    .badge {{
      display: inline-block;
      margin-bottom: 18px;
      padding: 10px 18px;
      border-radius: 999px;
      background: {palette["accent_soft"]};
      color: {palette["accent"]};
      font-size: 24px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .copy-small {{
      font-size: 34px;
      line-height: 1.25;
      font-weight: 500;
      color: {palette["ink_muted"]};
      letter-spacing: .06em;
      text-transform: uppercase;
      margin-bottom: 18px;
    }}
    .copy-large {{
      font-size: 92px;
      line-height: 1.02;
      font-weight: 900;
      letter-spacing: -0.045em;
      max-width: 760px;
      text-wrap: balance;
    }}
    .phone-shadow {{
      position: absolute;
      left: calc(50% + {variant["phone_shift_x"]});
      top: calc({variant["phone_top"]} + 28px);
      width: 748px;
      height: 1540px;
      transform: translateX(-50%) rotate({variant["phone_tilt"]});
      border-radius: 88px;
      background: rgba(18, 20, 26, 0.18);
      filter: blur(34px);
      z-index: 0;
    }}
    .phone {{
      position: absolute;
      left: calc(50% + {variant["phone_shift_x"]});
      top: {variant["phone_top"]};
      width: 748px;
      height: 1568px;
      transform: translateX(-50%) rotate({variant["phone_tilt"]});
      border-radius: 88px;
      background:
        linear-gradient(145deg, rgba(255,255,255,.98), rgba(244,246,250,.92));
      box-shadow:
        0 48px 96px rgba(18, 20, 26, 0.16),
        0 0 0 1px rgba(255,255,255,.72),
        inset 0 0 0 12px rgba(255,255,255,.96),
        inset 0 0 0 14px rgba(226, 230, 238, 0.88);
      z-index: 2;
    }}
    .phone::before {{
      content: "";
      position: absolute;
      left: 50%;
      top: 24px;
      width: 168px;
      height: 34px;
      transform: translateX(-50%);
      border-radius: 999px;
      background: rgba(12, 14, 18, 0.92);
      z-index: 4;
    }}
    .screen {{
      position: absolute;
      left: 52px;
      top: 56px;
      width: 644px;
      height: 1418px;
      border-radius: 48px;
      overflow: hidden;
      background: #ffffff;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.65);
    }}
    .screen img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      object-position: center top;
      display: block;
      filter: contrast(1.03) saturate(1.04);
    }}
    .glass {{
      pointer-events: none;
      position: absolute;
      inset: 0;
      border-radius: 88px;
      box-shadow:
        inset 0 1px 0 rgba(255,255,255,.98),
        inset 0 -18px 40px rgba(255,255,255,.08);
    }}
    .floor {{
      position: absolute;
      left: -10%;
      right: -10%;
      bottom: -120px;
      height: 320px;
      background: radial-gradient(circle at 50% 0%, {palette["accent_soft"]}, transparent 68%);
      z-index: 1;
    }}
  </style>
</head>
<body>
  <main class="canvas">
    <div class="grain"></div>
    <div class="accent-line"></div>
    <section class="copy">
      {badge_block}
      <div class="copy-small">{small_html}</div>
      <div class="copy-large">{large_html}</div>
    </section>
    <div class="floor"></div>
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


def render_douyin_deal_card_html(
    *,
    screenshot_path: Path,
    template: dict,
    copy: str,
    width: int,
    height: int,
) -> str:
    small, large = split_copy(
        copy,
        str(template.get("headline_small") or "额外加料！"),
        str(template.get("headline_large") or "同款更低价"),
    )
    img_src = image_data_uri(screenshot_path)
    small_html = html.escape(small)
    large_html = html.escape(large)
    shot_width = 700
    shot_height = 1539
    if Image is not None:
        try:
            with Image.open(screenshot_path) as img:
                source_width, source_height = img.size
            if source_width > 0 and source_height > 0:
                ratio = source_width / source_height
                max_width = 760
                max_height = 1540
                shot_width = min(max_width, int(round(max_height * ratio)))
                shot_height = int(round(shot_width / ratio))
        except Exception:
            pass
    card_width = shot_width + 104
    card_height = shot_height + 104
    float_height = 342
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
      background: #ff2367;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    }}
    .canvas {{
      position: relative;
      width: {width}px;
      height: {height}px;
      overflow: hidden;
      --shot-width: {shot_width}px;
      --shot-height: {shot_height}px;
      --card-width: {card_width}px;
      --card-height: {card_height}px;
      --float-width: calc(var(--card-width) + 132px);
      --float-height: {float_height}px;
      background:
        radial-gradient(circle at 10% 18%, rgba(255,255,255,.25) 0 2px, transparent 3px),
        radial-gradient(circle at 86% 18%, rgba(255,255,255,.18), transparent 32%),
        linear-gradient(158deg, #ff2b7c 0%, #ff225f 45%, #fa315d 72%, #ec1f5c 100%);
      background-size: 26px 26px, auto, auto;
    }}
    .canvas::before {{
      content: "";
      position: absolute;
      right: -48px;
      bottom: 58px;
      width: 442px;
      height: 442px;
      opacity: .18;
      transform: rotate(-16deg);
      background:
        linear-gradient(45deg, rgba(255,255,255,.55) 25%, transparent 25% 75%, rgba(255,255,255,.55) 75%),
        linear-gradient(45deg, rgba(255,255,255,.55) 25%, transparent 25% 75%, rgba(255,255,255,.55) 75%);
      background-position: 0 0, 20px 20px;
      background-size: 40px 40px;
      border-radius: 48px;
    }}
    .canvas::after {{
      content: "";
      position: absolute;
      left: -160px;
      top: 660px;
      width: 520px;
      height: 520px;
      border-radius: 50%;
      background: rgba(255,255,255,.10);
      filter: blur(2px);
    }}
    .tiny-copy {{
      position: absolute;
      top: 30px;
      left: 0;
      width: 100%;
      text-align: center;
      color: rgba(255,255,255,.62);
      font-size: 22px;
      letter-spacing: .18em;
      font-weight: 600;
    }}
    .deal-tag {{
      position: absolute;
      top: 164px;
      left: 50%;
      transform: translateX(-50%) rotate(-3deg);
      min-width: 210px;
      height: 64px;
      padding: 0 30px;
      border-radius: 10px;
      background: #090909;
      color: #fff;
      font-size: 34px;
      line-height: 64px;
      font-weight: 900;
      text-align: center;
      letter-spacing: -.03em;
      box-shadow: 0 10px 0 rgba(0,0,0,.16);
      z-index: 4;
    }}
    .deal-tag::after,
    .deal-tag::before {{
      content: "";
      position: absolute;
      right: -42px;
      background: #101010;
      transform-origin: left center;
    }}
    .deal-tag::before {{
      top: 4px;
      width: 54px;
      height: 12px;
      transform: rotate(24deg);
      border-radius: 8px;
    }}
    .deal-tag::after {{
      top: 34px;
      width: 38px;
      height: 10px;
      transform: rotate(-18deg);
      border-radius: 8px;
    }}
    .headline {{
      position: absolute;
      top: 238px;
      left: 0;
      width: 100%;
      color: #fff;
      text-align: center;
      font-size: 86px;
      line-height: .96;
      font-weight: 1000;
      letter-spacing: -.08em;
      text-shadow:
        0 5px 0 rgba(202, 22, 73, .42),
        0 18px 36px rgba(105, 0, 29, .22);
      z-index: 3;
    }}
    .headline::after {{
      content: "";
      position: absolute;
      left: 50%;
      bottom: -32px;
      width: 330px;
      height: 36px;
      transform: translateX(-50%) rotate(-4deg);
      background: #ffd753;
      clip-path: polygon(0 50%, 18% 34%, 36% 55%, 54% 30%, 72% 52%, 100% 35%, 100% 62%, 72% 77%, 54% 55%, 36% 82%, 18% 58%, 0 74%);
      opacity: .96;
      z-index: -1;
    }}
    .app-card {{
      position: absolute;
      left: 50%;
      top: 448px;
      width: var(--card-width);
      height: var(--card-height);
      transform: translateX(-50%);
      border-radius: 78px;
      background: #fff;
      box-shadow:
        0 50px 90px rgba(132, 0, 48, .30),
        inset 0 0 0 10px rgba(255,255,255,.82);
      overflow: visible;
      z-index: 2;
    }}
    .app-card::before {{
      content: "";
      position: absolute;
      inset: 18px;
      border-radius: 62px;
      border: 2px solid rgba(238, 56, 100, .22);
      pointer-events: none;
      z-index: 5;
    }}
    .screen-shell {{
      position: absolute;
      left: 50%;
      top: 52px;
      width: var(--shot-width);
      height: var(--shot-height);
      transform: translateX(-50%);
      border-radius: 38px;
      background: #fff;
      overflow: hidden;
      z-index: 2;
    }}
    .screen {{
      position: absolute;
      inset: 0;
      overflow: hidden;
      background: #fff;
      z-index: 1;
    }}
    .screen img {{
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
      object-position: center top;
    }}
    .canvas > .float-deal {{
      position: absolute;
      left: 50%;
      top: 836px;
      width: var(--float-width);
      height: var(--float-height);
      border-radius: 44px;
      background: white;
      box-shadow:
        0 46px 92px rgba(117, 0, 37, .34),
        0 18px 36px rgba(255, 37, 92, .22),
        0 0 0 5px rgba(255, 49, 101, .18);
      transform: translateX(-50%) rotate(-1.8deg) scale(1.045);
      transform-origin: center center;
      z-index: 8;
      overflow: hidden;
    }}
    .float-deal::before {{
      content: "";
      position: absolute;
      inset: 0;
      border-radius: 44px;
      background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(255,255,255,.94));
      box-shadow: inset 0 0 0 2px rgba(255, 61, 105, .12);
      z-index: -1;
    }}
    .float-deal .photo {{
      position: absolute;
      left: 32px;
      top: 34px;
      width: 218px;
      height: 218px;
      border-radius: 28px;
      background:
        radial-gradient(circle at 34% 30%, #f7d7a9, transparent 36%),
        linear-gradient(135deg, #b96f33, #f6d39a);
    }}
    .float-deal .copy {{
      position: absolute;
      left: 284px;
      top: 48px;
      right: 142px;
      color: #2b2022;
      font-size: 36px;
      line-height: 1.16;
      font-weight: 900;
    }}
    .float-deal .meta {{
      position: absolute;
      left: 284px;
      top: 138px;
      right: 142px;
      color: #96878b;
      font-size: 23px;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .float-deal .price {{
      position: absolute;
      left: 284px;
      bottom: 48px;
      color: #161012;
      font-size: 56px;
      font-weight: 950;
      letter-spacing: -.04em;
    }}
    .float-deal .price small {{
      color: #8c7c80;
      font-size: 21px;
      text-decoration: line-through;
      margin-left: 8px;
    }}
    .save-ribbon {{
      position: absolute;
      left: -22px;
      bottom: 26px;
      width: 236px;
      height: 70px;
      transform: rotate(-6deg);
      border-radius: 0 28px 28px 0;
      background: linear-gradient(90deg, #ff2a66, #ff6b86);
      color: #fff;
      text-align: center;
      line-height: 70px;
      font-weight: 1000;
      font-size: 34px;
      text-shadow: 0 2px 0 rgba(150,0,42,.24);
    }}
    .float-deal .grab-main {{
      position: absolute;
      right: 20px;
      top: 118px;
      width: 124px;
      height: 124px;
      border-radius: 28px;
      background: linear-gradient(180deg, #ff457a, #ff1d5b);
      color: #fff;
      text-align: center;
      line-height: 124px;
      font-size: 70px;
      font-weight: 1000;
      box-shadow: 0 18px 30px rgba(219, 0, 64, .28);
    }}
    .grab {{
      position: absolute;
      right: 42px;
      border-radius: 16px;
      width: 68px;
      height: 68px;
      background: linear-gradient(180deg, #ff457a, #ff1d5b);
      color: #fff;
      text-align: center;
      line-height: 68px;
      font-size: 38px;
      font-weight: 1000;
      box-shadow: 0 18px 30px rgba(219, 0, 64, .28);
      z-index: 5;
    }}
    .grab.g1 {{ top: 692px; }}
    .grab.g2 {{ top: 950px; }}
    .grab.g3 {{ top: 1208px; }}
  </style>
</head>
<body>
  <main class="canvas">
    <div class="tiny-copy">图片仅供示例，以 APP 内实际展示为准</div>
    <div class="deal-tag">{small_html}</div>
    <section class="headline">{large_html}</section>
    <section class="app-card">
      <div class="screen-shell">
        <div class="screen"><img src="{img_src}" alt=""></div>
        <div class="grab g1">抢</div>
        <div class="grab g2">抢</div>
        <div class="grab g3">抢</div>
      </div>
    </section>
    <div class="float-deal">
      <div class="photo"></div>
      <div class="copy">今日好价<br>附近热门推荐</div>
      <div class="meta">&lt;100m 精选优惠 · 热销3万+</div>
      <div class="price">¥16.2<small>¥30.2</small></div>
      <div class="save-ribbon">超省价!</div>
      <div class="grab-main">抢</div>
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
            index=index,
        )
    if layout == "purple-live-phone":
        return render_purple_live_html(
            screenshot_path=screenshot_path,
            template=template,
            copy=copy,
            width=width,
            height=height,
        )
    if layout == "douyin-deal-card":
        return render_douyin_deal_card_html(
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
    template_name = html.escape(str(jobs[0]["template"])) if jobs else ""
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
    .back-link {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 14px;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,.88);
      box-shadow: 0 8px 24px rgba(25,31,45,.08);
      color: #2868f0;
      font-size: 14px;
      font-weight: 700;
      text-decoration: none;
    }}
    .back-link:hover {{
      background: #fff;
    }}
    .header-meta {{
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 10px 16px;
    }}
    .template-tag {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 999px;
      background: rgba(40,104,240,.10);
      color: #2868f0;
      font-size: 12px;
      font-weight: 700;
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
    .gen-overlay {{
      position: fixed;
      inset: 0;
      z-index: 9999;
      display: grid;
      place-items: center;
      background: rgba(12, 16, 24, .58);
      backdrop-filter: blur(8px);
      opacity: 1;
      transition: opacity .25s ease;
    }}
    .gen-overlay.hidden {{
      opacity: 0;
      pointer-events: none;
    }}
    .gen-panel {{
      width: min(420px, calc(100vw - 48px));
      padding: 28px 26px 24px;
      border-radius: 24px;
      background: rgba(255,255,255,.96);
      box-shadow: 0 28px 80px rgba(0,0,0,.28);
      text-align: center;
    }}
    .gen-panel strong {{
      display: block;
      font-size: 20px;
      margin-bottom: 10px;
    }}
    #gen-message {{
      margin: 0 0 18px;
      color: #626977;
      font-size: 14px;
      line-height: 1.5;
      min-height: 42px;
    }}
    .gen-progress {{
      height: 8px;
      border-radius: 999px;
      background: #e8ecf3;
      overflow: hidden;
      margin-bottom: 10px;
    }}
    #gen-bar {{
      height: 100%;
      width: 0%;
      border-radius: inherit;
      background: linear-gradient(90deg, #2868f0, #7c5cff);
      transition: width .25s ease;
    }}
    #gen-count {{
      margin: 0;
      color: #768092;
      font-size: 13px;
    }}
    .spinner {{
      width: 42px;
      height: 42px;
      margin: 0 auto 16px;
      border-radius: 50%;
      border: 3px solid #e8ecf3;
      border-top-color: #2868f0;
      animation: spin .9s linear infinite;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
  </style>
</head>
<body>
  <header>
    <a class="back-link" href="/">← 返回选模板</a>
    <div class="header-meta">
      <h1>Market Preview</h1>
      {f'<span class="template-tag">{template_name}</span>' if template_name else ''}
    </div>
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
  <div id="gen-overlay" class="gen-overlay hidden" aria-hidden="true">
    <div class="gen-panel" role="status" aria-live="polite">
      <div class="spinner"></div>
      <strong id="gen-title">正在生成市场图</strong>
      <p id="gen-message">准备中，请勿关闭页面…</p>
      <div class="gen-progress"><div id="gen-bar"></div></div>
      <p id="gen-count">0 / 0</p>
    </div>
  </div>
  <script>
    const button = document.querySelector('#confirm');
    const status = document.querySelector('#status');
    const overlay = document.querySelector('#gen-overlay');
    const genMessage = document.querySelector('#gen-message');
    const genBar = document.querySelector('#gen-bar');
    const genCount = document.querySelector('#gen-count');

    function showOverlay() {{
      overlay.classList.remove('hidden');
      overlay.setAttribute('aria-hidden', 'false');
    }}

    function hideOverlay() {{
      overlay.classList.add('hidden');
      overlay.setAttribute('aria-hidden', 'true');
    }}

    function updateProgress(evt) {{
      const total = Number(evt.total || 0);
      const current = Number(evt.current || 0);
      if (evt.message) genMessage.textContent = evt.message;
      if (total > 0) {{
        genCount.textContent = current + ' / ' + total;
        const pct = Math.max(4, Math.min(100, Math.round((current / total) * 100)));
        genBar.style.width = pct + '%';
      }}
    }}

    async function readProgressStream(res) {{
      if (!res.body || !res.body.getReader) {{
        const text = await res.text();
        if (!res.ok) throw new Error(text);
        return JSON.parse(text);
      }}
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let finalPayload = null;
      while (true) {{
        const {{ done, value }} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {{ stream: true }});
        const lines = buffer.split('\\n');
        buffer = lines.pop() || '';
        for (const line of lines) {{
          if (!line.trim()) continue;
          const evt = JSON.parse(line);
          if (evt.type === 'progress') updateProgress(evt);
          if (evt.type === 'result') finalPayload = evt.data;
          if (evt.type === 'error') throw new Error(evt.message || '生成失败');
        }}
      }}
      if (!finalPayload) throw new Error('未收到生成结果');
      return finalPayload;
    }}

    button.addEventListener('click', async () => {{
      button.disabled = true;
      showOverlay();
      genMessage.textContent = '正在准备 HTML 预览…';
      genBar.style.width = '4%';
      genCount.textContent = '0 / 0';
      status.textContent = '正在截图导出，请稍等…';
      try {{
        const res = await fetch('/api/confirm-generate', {{
          method: 'POST',
          headers: {{ 'Accept': 'application/x-ndjson' }},
        }});
        const data = await readProgressStream(res);
        const failed = data.failed_jobs || [];
        if (failed.length) {{
          genMessage.textContent = '部分失败：' + failed.map(item => item.index).join(', ');
          status.textContent = genMessage.textContent;
        }} else {{
          genMessage.textContent = '已完成，共生成 ' + (data.outputs || []).length + ' 张 PNG';
          status.textContent = genMessage.textContent;
          genBar.style.width = '100%';
        }}
        setTimeout(hideOverlay, failed.length ? 1800 : 900);
      }} catch (error) {{
        genMessage.textContent = '生成失败：' + error.message;
        status.textContent = genMessage.textContent;
        setTimeout(hideOverlay, 2200);
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
          <a class="button secondary" href="/preview">返回预览</a>
          <a class="button secondary" href="/">返回选模板</a>
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
    selected = str(state.get("selected_template_name") or "")
    selected_pack = str(state.get("pack", {}).get("pack_name") or "")
    selected_banner = ""
    if selected:
        selected_banner = f"""
    <p class="current">
      当前已选：<strong>{html.escape(selected)}</strong>
      {f'（{html.escape(selected_pack)}）' if selected_pack else ''}
      <a href="/preview">继续预览</a>
    </p>"""
    cards: list[str] = []
    for item in iter_catalog_templates():
        name = item["template_name"]
        layout = item["layout"]
        prompt = item["prompt"]
        pack_name = item["pack_name"]
        cards.append(
            f"""
      <article class="card">
        <p class="pack-tag">{html.escape(pack_name)}</p>
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
    .current {{
      margin: 10px 0 0;
      color: #626977;
      font-size: 14px;
    }}
    .current a {{
      margin-left: 12px;
      color: #2868f0;
      font-weight: 700;
      text-decoration: none;
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
    .pack-tag {{
      margin: 0;
      color: #ff2d77;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .04em;
      text-transform: uppercase;
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
    <p>先选一套模板，系统再生成 HTML 预览。预览满意后点击底部确认按钮导出 PNG。</p>{selected_banner}
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


def _emit_progress(on_progress, **payload) -> None:
    if on_progress:
        on_progress(payload)


def generate_png_outputs(state: dict, on_progress=None) -> dict:
    _emit_progress(
        on_progress,
        step="prepare",
        message="正在刷新 HTML 预览…",
        current=0,
        total=len(state.get("screenshots") or []),
    )
    chrome = find_chrome()
    render_all_preview_html(state)
    jobs = state.get("jobs") or []
    total = len(jobs)
    _emit_progress(
        on_progress,
        step="chrome",
        message="已启动 Chrome，开始逐张截图…",
        current=0,
        total=total,
    )
    outputs: list[dict] = []
    failed: list[dict] = []
    for step_index, job in enumerate(jobs, start=1):
        html_path = Path(job["html"])
        output_path = Path(job["output"])
        _emit_progress(
            on_progress,
            step="screenshot",
            message=f"正在截图 {job['index']}（{step_index}/{total}）…",
            current=step_index - 1,
            total=total,
            index=job["index"],
        )
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
        _emit_progress(
            on_progress,
            step="screenshot",
            message=f"已完成 {job['index']}（{step_index}/{total}）",
            current=step_index,
            total=total,
            index=job["index"],
        )
    _emit_progress(
        on_progress,
        step="done",
        message="全部截图任务结束",
        current=total,
        total=total,
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

    def send_ndjson_line(self, payload: dict) -> None:
        self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def send_generate_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def on_progress(payload: dict) -> None:
            self.send_ndjson_line({"type": "progress", **payload})

        try:
            result = generate_png_outputs(self.state, on_progress=on_progress)
            self.send_ndjson_line({"type": "result", "data": result})
        except Exception as exc:
            self.send_ndjson_line({"type": "error", "message": str(exc)})

    def send_text(self, text: str, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.send_text(render_template_selection_page(self.state))
            return
        select_match = re.fullmatch(r"/select/(.+)", parsed.path)
        if select_match:
            try:
                name = urllib.parse.unquote(select_match.group(1))
                pack, _ = resolve_template_selection(name)
                self.state["pack"] = pack
                self.state["width"], self.state["height"] = canvas_size(pack)
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
                accept = (self.headers.get("Accept") or "").lower()
                if "application/x-ndjson" in accept:
                    self.send_generate_stream()
                else:
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
