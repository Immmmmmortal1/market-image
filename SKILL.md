---
name: market-reference-pack-generator
description: Use when the user wants to generate app market screenshots from a local screenshots folder using a predefined prompt/layout template pack rendered as HTML screenshots. Trigger on 市场图生成, 提示词模板包, HTML 截图生成, Google Play market image, or when the user provides numbered app screenshots.
metadata:
  version: 3.3.0
---

# Market Reference Pack Generator

## Overview

This skill is a `local HTML prompt-template market image generator`.

Its job is to:

- take the user's numbered app screenshots from a required local folder
- start a local web server for template selection
- let the user choose one prompt/layout template in the browser
- render a local HTML market-image page using the screenshot
- allow limited per-image editing in the preview server
- wait for the user's browser confirmation before screenshot export
- screenshot the approved HTML pages with local Chrome
- save generated market images into `<screenshots-dir>/newImage/`

It does not require Gemini, imagegen, or any API key.

## Required Inputs

- `screenshots_dir` - required. Folder containing numbered screenshots such as `01.png`, `02.jpg`, `03.jpeg`, or `04.webp`.
- `prompt_pack_dir` - optional. Folder containing a prompt template pack with `pack.json`. Defaults to `prompt_packs/default-googleplay-prompt-pack`.

Optional input:

- `copy_json` or `copy_file` - optional copy values. Use `{"default":"小字|大字"}` or per-image keys such as `{"01":"发现|多元兴趣方式"}`.
- `port` - optional preview server port. Defaults to `8765`.

## Prompt Pack Contract

A prompt pack is a folder with `pack.json`.

`pack.json` must declare:

- `pack_name`
- `mode`: must be `html_screenshot_generation`
- `canvas.width`
- `canvas.height`
- `templates`: one or more prompt/layout templates

Each template must declare:

- `name`
- `layout`
- optional `headline_small`
- optional `headline_large`
- optional `prompt` for human-readable design intent

The default supported layout is:

- `clean-discover-phone`
- `purple-live-phone`
- `douyin-deal-card`
- `spotify-pink-series`
- `element-dark-glow-series`

## Use When

Use when:

- the user asks to generate market screenshots from local app screenshots
- the user says the skill should use a prompt template pack
- the user provides a screenshots folder
- the user wants generated PNG market images saved in `newImage`
- the user expects local HTML rendering instead of API image generation

Do not use when:

- the user wants to call Gemini or another image model directly
- the user wants pixel-perfect replacement into a finished static image
- the user wants only to download Google Play reference screenshots
- the user only wants to copy screenshots unchanged

## Primary Boundary

This skill must render local HTML from prompt/layout templates.

It must not:

- require `GEMINI_API_KEY`
- call `google-nano-banana-2-imagegen`
- call remote image-generation APIs
- use `template_packs`
- use `compose_template_pack.py`
- paste screenshots into old local template images with coordinates
- ignore the user's provided screenshots

Allowed operations:

- select a prompt/layout template pack
- render HTML from the selected pack
- embed the user's screenshots into the phone-screen area
- preserve each provided screenshot's original aspect ratio
- replace small/large marketing copy when provided
- screenshot HTML with local Chrome
- start a local preview server for checking generated HTML before PNG export
- edit only approved per-image fields in preview mode

Editable fields:

- title text
- screenshot X/Y position
- screenshot scale
- background color

Non-editable fields:

- layout structure
- decorations
- phone frame
- typography system
- canvas size
- template-specific visual hierarchy

## Google Play Composition Rules

Every generated market image must follow Google Play vertical market image rules first.

The default canvas is `1242x2208`. Templates may change style, background, copy, frame, and decorative overlays, but they must keep the market-image canvas and the app screenshot visually balanced.

Screenshot ratio rules:

- Always preserve the original screenshot aspect ratio.
- Prefer `object-fit: contain` or computed width/height that matches the source screenshot ratio.
- Do not stretch, squash, or horizontally widen screenshots to fill a decorative frame.
- Do not crop important app UI unless the template explicitly frames a deliberate close-up and the user approves.
- If a screenshot is taller than the available visual area, scale it proportionally and let the surrounding frame/card adapt.

Composition balance rules:

- The screenshot area must be large enough to feel like the product hero, not a small pasted asset.
- The screenshot, title, decorative frame, and floating stickers must share one coherent visual hierarchy.
- Floating 3D labels, badges, cards, or buttons are allowed only as supporting decoration.
- Floating elements must not dominate the app screenshot, hide key UI, or make the final image look like a crude collage.
- Decorative overlays should echo the reference style while respecting Google Play proportions.

Spotlight floating-layer rules:

- A spotlight layer may be intentionally exaggerated when the template's purpose is to highlight a key in-app item.
- It should read as content enlarged from inside the screenshot, not as an unrelated poster sticker.
- It may break out of the screenshot area and even beyond the white phone/card background when the reference style calls for a strong 3D pop-out.
- Even when breaking out, it must remain visually anchored to an in-app location and preserve the underlying screenshot ratio.
- For deal/ecommerce templates, the spotlight layer should be wide, bold, shadowed, and promotional enough to become the main selling point.

Before accepting a new template, verify:

- The output is still `1242x2208` unless a different Google Play canvas is explicitly requested.
- The embedded screenshot keeps its source aspect ratio.
- The screenshot occupies a coordinated portion of the canvas.
- Overlays are scaled and positioned as part of the composition, not randomly pasted on top.

## Workflow

1. Confirm required screenshot folder.
   - The user must provide a local screenshots path.
   - If no screenshots path is provided, stop and ask for it.

2. Resolve prompt pack.
   - If the user provides `prompt_pack_dir`, use it.
   - If no prompt pack is provided, use:

```bash
~/.codex/skills/market-reference-pack-generator/prompt_packs/default-googleplay-prompt-pack
```

3. Validate prompt pack.
   - Confirm `pack.json` exists.
   - Confirm `mode` is `html_screenshot_generation`.
   - Confirm `canvas.width` and `canvas.height` exist.
   - Confirm `templates` is non-empty.
   - Confirm each template has `name` and `layout`.

4. Read screenshots in order.
   - Accept numbered image files such as `01.png`, `02.jpg`, `03.jpeg`, `04.webp`.
   - Sort by numeric filename.
   - Use screenshots in that order.

5. Start the browser workflow.
   - Use `scripts/run_prompt_pack.py --serve-preview`.
   - The first web page must be a template selection page.
   - Do not render final PNG screenshots before the user confirms in the browser.

6. Template selection.
   - User selects one template in the browser.
   - After selection, render HTML previews for all numbered screenshots using that selected template.
   - Each numbered screenshot becomes one local HTML page.
   - The screenshot must be embedded as the app screenshot, not described from memory.

7. Preview and edit.
   - Show the generated HTML previews in the browser.
   - Click `Edit` on a preview card to modify only title text, screenshot position/scale, and background color.
   - Save edits into `<output-dir>/overrides.json`.
   - Keep all non-editable template structure locked.

8. Confirm and export.
   - The preview page must provide a bottom `确认生成截图` button.
   - When the user clicks it, the server screenshots the approved HTML pages with local Chrome.
   - Output directory is `<screenshots-dir>/newImage/` unless `--output-dir` is provided.
   - HTML previews are saved into `<output-dir>/_html/`.
   - If `<output-dir>/overrides.json` exists, apply those edits before screenshot export.
   - Output files are named `01.png`, `02.png`, `03.png`.

9. Return an artifact report.
   - Include prompt pack used.
   - Include input screenshot order.
   - Include generated HTML paths.
   - Include output PNG paths.
   - Include any failed jobs.

## Commands

List built-in prompt packs:

```bash
python3 ~/.codex/skills/market-reference-pack-generator/scripts/run_prompt_pack.py --list-packs
```

Preview generation jobs without writing PNG files:

```bash
python3 ~/.codex/skills/market-reference-pack-generator/scripts/run_prompt_pack.py \
  --screenshots-dir /absolute/path/to/screenshots \
  --dry-run
```

Start the required browser workflow:

```bash
python3 ~/.codex/skills/market-reference-pack-generator/scripts/run_prompt_pack.py \
  --screenshots-dir /absolute/path/to/screenshots \
  --serve-preview
```

Preview editor behavior:

- First choose a template in the browser.
- Review the generated HTML previews.
- Click `Edit` on any card if changes are needed.
- Change title text, screenshot X/Y, screenshot scale, or background color.
- Click `保存并刷新预览`.
- The edit is saved to `<output-dir>/overrides.json`.
- Click the bottom `确认生成截图` button to export PNGs with the saved edits.

Start preview on a custom port:

```bash
python3 ~/.codex/skills/market-reference-pack-generator/scripts/run_prompt_pack.py \
  --screenshots-dir /absolute/path/to/screenshots \
  --serve-preview \
  --port 8899
```

Direct CLI screenshot export is an advanced fallback, not the default flow:

```bash
python3 ~/.codex/skills/market-reference-pack-generator/scripts/run_prompt_pack.py \
  --screenshots-dir /absolute/path/to/screenshots \
  --generate
```

Generate with copy replacement:

```bash
python3 ~/.codex/skills/market-reference-pack-generator/scripts/run_prompt_pack.py \
  --screenshots-dir /absolute/path/to/screenshots \
  --copy-json '{"default":"发现|多元兴趣方式"}' \
  --generate
```

## Fallback Behavior

If `screenshots_dir` is missing:

- stop
- ask the user for the screenshots folder path

If `prompt_pack_dir` is missing:

- use the built-in `default-googleplay-prompt-pack`

If `pack.json` is missing or invalid:

- stop
- report the exact prompt pack problem

If no numbered screenshots are found:

- stop
- report accepted names such as `01.png` or `01.jpg`

If local Chrome is missing:

- stop
- report that Chrome/Chromium is required for local HTML screenshot rendering

If preview port is already in use:

- stop
- report the port conflict
- retry with `--port` using another local port

## Output Contract

When listing available prompt packs:

- `Mode`: `list_prompt_packs`
- `Available Prompt Packs`
- `Default Prompt Pack`

When previewing jobs:

- `Mode`: `html_prompt_pack_dry_run`
- `Prompt Pack`
- `Canvas`
- `Input Screenshot Order`
- `Jobs`

When starting the preview server:

- `Mode`: `html_prompt_pack_preview_server`
- `URL`
- `Template Selection Page`
- `Serving Directory`
- `Stop Instruction`

When a template is selected in the browser:

- generate HTML previews for all numbered screenshots
- write `<output-dir>/index.html`
- show preview cards and the bottom confirm button

When edits are saved:

- update `<output-dir>/overrides.json`
- re-render the edited HTML file
- keep all non-editable template structure unchanged

When generation succeeds:

- `Mode`: `html_prompt_pack_generation`
- `Prompt Pack`
- `Selected Template`
- `Renderer`
- `Input Screenshot Order`
- `HTML Files`
- `Output PNGs`
- `Failed Jobs`

## Hard Rule

This skill is local HTML rendering.

Never sacrifice screenshot proportion for decoration. A market image is acceptable only when the user-provided screenshot keeps its original aspect ratio and the full composition looks intentionally designed for Google Play, not assembled from mismatched pasted parts.

If a request says `模板包` in this skill, interpret it as `提示词/版式模板包`, not a folder of finished market images and not a remote image model prompt.
