# Prompt Packs

Prompt packs replace the old image template packs.

Each pack is a folder with `pack.json`. The pack defines reusable prompt/layout templates that are selected in the local preview server, rendered into HTML previews, optionally edited with limited per-image overrides, and exported to PNG only after browser confirmation.

Required fields:

- `pack_name`
- `mode`: `html_screenshot_generation`
- `canvas`
- `templates`

Supported layouts:

- `clean-discover-phone`
- `purple-live-phone`
- `spotify-pink-series`
- `element-dark-glow-series`

Built-in packs:

- `default-googleplay-prompt-pack`
