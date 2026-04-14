# astrbot_plugin_what2eat

A dinner assistant plugin for AstrBot.

## Features

- Multi-category meal picking with independent trees (`main`, `side`, `drink`, ...).
- Flavor toggles that can be enabled/disabled and used by picker.
- Plugin config is managed by AstrBot built-in plugin config panel via `_conf_schema.json`.
- Legacy state migration is supported from `data/plugin_data/astrbot_plugin_what2eat/state.json`.

## Commands

- `/what2eat`: draw one result path for each enabled category.
- `/what2eat_flavors`: show current flavor list and active flavor toggles.

## Dashboard API Routes

- `GET /api/plug/what2eat/state`
- `POST /api/plug/what2eat/state`
- `POST /api/plug/what2eat/toggles`
- `POST /api/plug/what2eat/pick`

## Notes

- The plugin never uses `requests`.
- Invalid or malformed state is normalized automatically to safe defaults.
