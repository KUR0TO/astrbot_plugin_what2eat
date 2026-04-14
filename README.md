# astrbot_plugin_what2eat

A dinner assistant plugin for AstrBot.

## Features

- Multi-category meal picking with independent trees (`main`, `side`, `drink`, ...).
- Flavor toggles that can be enabled/disabled and used by picker and AI generation.
- Persistent state in AstrBot data directory: `data/plugin_data/astrbot_plugin_what2eat/state.json`.
- Dashboard custom panel for managing categories, flavors, provider, and AI suggestions.
- AI suggestion endpoint appends generated items into the selected tree node.

## Commands

- `/what2eat`: draw one result path for each enabled category.
- `/what2eat_flavors`: show current flavor list and active flavor toggles.

## Dashboard API Routes

- `GET /api/plug/what2eat/state`
- `POST /api/plug/what2eat/state`
- `POST /api/plug/what2eat/toggles`
- `GET /api/plug/what2eat/providers`
- `POST /api/plug/what2eat/pick`
- `POST /api/plug/what2eat/ai_suggest`

## Notes

- The plugin never uses `requests`; all model calls go through AstrBot provider APIs.
- Invalid or malformed state is normalized automatically to safe defaults.
