# Web Service

This folder contains the browser workflow entrypoint.

Use `server.py` when you want the intended user-facing flow:

- start a local web server
- select a template in the browser
- preview generated HTML market images
- edit only approved fields
- click `确认生成截图` to export PNG files

The implementation delegates to `../scripts/run_prompt_pack.py` so the skill has one rendering engine and one browser workflow.
