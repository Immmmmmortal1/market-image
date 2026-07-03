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
- `douyin-deal-card`
- `spotify-pink-series`
- `element-dark-glow-series`

Built-in packs:

- `default-googleplay-prompt-pack`

## Template Composition Rules

All prompt templates must preserve the user's screenshot aspect ratio. Do not stretch, squash, or force screenshots into a decorative frame with the wrong width/height.

For Google Play vertical assets, the default canvas is `1242x2208`. A template should coordinate the screenshot size, title area, frame, whitespace, and overlays so the final image feels designed as one poster. Floating 3D labels and badges are allowed, but they must support the screenshot instead of covering key UI or looking pasted on.

For promotional spotlight templates, the floating layer can be deliberately exaggerated. It should feel like a specific item from the screenshot has been magnified and lifted out of the app UI. It may break out of the screenshot and white phone/card background, but it must stay visually anchored to the app content and preserve the screenshot's original aspect ratio.
