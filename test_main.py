from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


class _DummyRequest:
    async def get_json(self, silent=True):
        _ = silent
        return None


def _noop_decorator(*args, **kwargs):
    _ = args, kwargs

    def _inner(func):
        return func

    return _inner


class _DummyStar:
    def __init__(self, context):
        self.context = context


class _DummyStarTools:
    @staticmethod
    def initialize(context):
        _ = context

    @staticmethod
    def get_data_dir(name):
        _ = name
        return Path(".")


# Inject minimal stubs so the plugin module can be imported without full runtime deps.
quart_mod = types.ModuleType("quart")
quart_mod.jsonify = lambda data: data
quart_mod.request = _DummyRequest()
sys.modules["quart"] = quart_mod

astrbot_mod = types.ModuleType("astrbot")
api_mod = types.ModuleType("astrbot.api")
api_mod.AstrBotConfig = dict
api_mod.logger = _DummyLogger()

event_mod = types.ModuleType("astrbot.api.event")
event_mod.AstrMessageEvent = object
event_mod.filter = types.SimpleNamespace(command=_noop_decorator)

star_mod = types.ModuleType("astrbot.api.star")
star_mod.Context = object
star_mod.Star = _DummyStar
star_mod.StarTools = _DummyStarTools
star_mod.register = _noop_decorator

sys.modules["astrbot"] = astrbot_mod
sys.modules["astrbot.api"] = api_mod
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.star"] = star_mod

MODULE_PATH = Path(__file__).with_name("main.py")
MODULE_SPEC = importlib.util.spec_from_file_location(
    "what2eat_plugin_main",
    MODULE_PATH,
)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError("failed to load plugin module")
PLUGIN_MODULE = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = PLUGIN_MODULE
MODULE_SPEC.loader.exec_module(PLUGIN_MODULE)

What2EatPlugin = PLUGIN_MODULE.What2EatPlugin
default_state = PLUGIN_MODULE.default_state
normalize_state = PLUGIN_MODULE.normalize_state


class _DummyContext:
    def register_web_api(self, **kwargs):
        return None


class _DummyConfig(dict):
    def save_config(self, replace_config: dict | None = None):
        if replace_config is not None:
            self.clear()
            self.update(replace_config)


def _make_plugin(tmp_path: Path, initial_config: dict | None = None) -> What2EatPlugin:
    _ = tmp_path
    cfg = _DummyConfig()
    cfg.save_config(initial_config or default_state())
    return What2EatPlugin(context=_DummyContext(), config=cfg)


def test_normalize_state_invalid_input_fallback():
    state = normalize_state(None)
    assert state["version"] == 1
    assert len(state["categories"]) == 3


def test_normalize_state_filters_invalid_flavor_refs():
    raw = default_state()
    raw["flavors"] = [{"id": "spicy", "label": "Spicy"}]
    raw["toggles"]["flavors"] = ["spicy", "not-exists"]

    state = normalize_state(raw)
    assert state["toggles"]["flavors"] == ["spicy"]


def test_normalize_state_supports_dict_style_categories_and_flavors():
    raw = {
        "categories": {
            "main": {
                "label": "Main",
                "enabled": True,
                "tree": {
                    "id": "root",
                    "label": "Main",
                    "weight": 1,
                    "flavors": [],
                    "children": [],
                    "items": [],
                },
            }
        },
        "flavors": {
            "spicy": {
                "label": "Spicy",
                "description": "hot",
                "enabled_by_default": False,
            }
        },
        "toggles": {
            "categories": {"main": True},
            "flavors": ["spicy"],
        },
    }

    state = normalize_state(raw)
    assert state["categories"][0]["id"] == "main"
    assert state["flavors"][0]["id"] == "spicy"
    assert state["toggles"]["flavors"] == ["spicy"]


def test_pick_result_respects_category_toggles(tmp_path: Path):
    cfg = default_state()
    cfg["categories"] = [
        {
            "id": "main",
            "label": "Main",
            "enabled": True,
            "tree": {
                "id": "root",
                "label": "Main",
                "weight": 1,
                "flavors": [],
                "children": [],
                "items": [
                    {
                        "id": "item",
                        "label": "Rice",
                        "weight": 1,
                        "flavors": [],
                        "children": [],
                        "items": [],
                    }
                ],
            },
        }
    ]
    cfg["toggles"]["categories"] = {"main": False}

    plugin = _make_plugin(tmp_path, cfg)
    picked = plugin._build_pick_result()
    assert picked == []


def test_pick_result_flavor_filter_fallback_to_all(tmp_path: Path):
    cfg = default_state()
    cfg["categories"] = [
        {
            "id": "main",
            "label": "Main",
            "enabled": True,
            "tree": {
                "id": "root",
                "label": "Main",
                "weight": 1,
                "flavors": [],
                "children": [],
                "items": [
                    {
                        "id": "a",
                        "label": "A",
                        "weight": 1,
                        "flavors": ["spicy"],
                        "children": [],
                        "items": [],
                    },
                    {
                        "id": "b",
                        "label": "B",
                        "weight": 1,
                        "flavors": ["sweet"],
                        "children": [],
                        "items": [],
                    },
                ],
            },
        }
    ]
    cfg["flavors"] = [{"id": "spicy", "label": "Spicy"}]
    cfg["toggles"] = {"categories": {"main": True}, "flavors": ["spicy"]}

    plugin = _make_plugin(tmp_path, cfg)
    result = plugin._build_pick_result(flavor_filter={"not-exist"})
    assert len(result) == 1
    assert result[0]["category_id"] == "main"


def test_load_and_migrate_state_migrates_when_config_default(tmp_path: Path):
    plugin = _make_plugin(tmp_path, default_state())
    plugin._state_path = tmp_path / "state.json"

    legacy = default_state()
    legacy["categories"][0]["label"] = "MigratedMain"
    plugin._state_path.write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    asyncio.run(plugin._load_and_migrate_state())

    assert plugin.config["categories"][0]["label"] == "MigratedMain"


def test_load_and_migrate_state_keeps_non_default_config(tmp_path: Path):
    non_default = default_state()
    non_default["categories"][0]["label"] = "CurrentConfig"

    plugin = _make_plugin(tmp_path, non_default)
    plugin._state_path = tmp_path / "state.json"

    legacy = default_state()
    legacy["categories"][0]["label"] = "LegacyConfig"
    plugin._state_path.write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    asyncio.run(plugin._load_and_migrate_state())

    assert plugin.config["categories"][0]["label"] == "CurrentConfig"
