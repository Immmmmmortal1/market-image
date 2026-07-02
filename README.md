# Market Image Generator

Local market screenshot generator for app store assets.

The main workflow is browser-first:

1. Provide a local folder of numbered screenshots, such as `01.png`, `02.jpg`, `03.webp`.
2. Start the local web service.
3. Choose a market image template in the browser.
4. Preview generated HTML market images.
5. Optionally edit title text, screenshot position/scale, and background color.
6. Click `确认生成截图` in the browser to export PNG files.

No Gemini key, image-generation API, or remote rendering service is required.

## Start Web Service

```bash
python3 web/server.py --screenshots-dir /absolute/path/to/screenshots
```

Default local URL:

```text
http://127.0.0.1:8765/
```

## Direct Runner

```bash
python3 scripts/run_prompt_pack.py \
  --screenshots-dir /absolute/path/to/screenshots \
  --serve-preview
```

PNG outputs are written to `<screenshots-dir>/newImage/` by default.
