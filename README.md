# astrbot_plugin_what2eat

一个用于“今天吃什么”的 AstrBot 晚餐助手插件。

## 功能特性

- 支持多分类独立抽取（如主食、配菜、饮料）。
- 支持口味标签与口味筛选开关。
- 通过 AstrBot 插件配置面板可视化配置（基于 `_conf_schema.json`）。
- 兼容旧版状态文件，启动时可自动迁移 `data/plugin_data/astrbot_plugin_what2eat/state.json`。

## 指令

- `/what2eat`：按已启用分类各随机抽取一条结果。
- `/what2eat_flavors`：显示当前口味列表与启用状态。

## 插件 API 路由

- `GET /api/plug/what2eat/state`
- `POST /api/plug/what2eat/state`
- `POST /api/plug/what2eat/toggles`
- `POST /api/plug/what2eat/pick`

## 说明

- 插件不使用 `requests`。
- 对异常或不完整配置会自动规范化并回退到安全结构。
