from __future__ import annotations

import asyncio
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quart import jsonify, request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter as event_filter
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_what2eat"
STORAGE_FILENAME = "state.json"
DEFAULT_CATEGORY_TEMPLATES = (
    ("main", "Main", "rice/noodle/bread"),
    ("side", "Side", "vegetables/meat"),
    ("drink", "Drink", "tea/juice/soda"),
)


def _ok(data: dict[str, Any] | list | None = None, message: str | None = None):
    if data is None:
        data = {}
    return jsonify({"status": "ok", "message": message, "data": data})


def _error(message: str):
    return jsonify({"status": "error", "message": message, "data": None})


def _deep_equal(a: Any, b: Any) -> bool:
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == json.dumps(
        b,
        ensure_ascii=False,
        sort_keys=True,
    )


@dataclass
class FoodNode:
    id: str
    label: str
    weight: int = 1
    flavors: list[str] = field(default_factory=list)
    children: list[FoodNode] = field(default_factory=list)
    items: list[FoodNode] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FoodNode:
        return cls(
            id=str(data.get("id") or _gen_id("node")),
            label=str(data.get("label") or ""),
            weight=_safe_weight(data.get("weight", 1)),
            flavors=_clean_str_list(data.get("flavors", [])),
            children=[cls.from_dict(child) for child in data.get("children", [])],
            items=[cls.from_dict(item) for item in data.get("items", [])],
        )


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _safe_weight(weight: Any) -> int:
    try:
        return max(1, int(weight))
    except (TypeError, ValueError):
        return 1


def _clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    ret: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ret.append(text)
    return ret


def _default_tree(category_id: str, label: str, hint: str) -> dict[str, Any]:
    return {
        "id": f"root_{category_id}",
        "label": label,
        "weight": 1,
        "flavors": [],
        "children": [],
        "items": [
            {
                "id": f"item_{category_id}",
                "label": hint,
                "weight": 1,
                "flavors": [],
                "children": [],
                "items": [],
            }
        ],
    }


def default_state() -> dict[str, Any]:
    categories = []
    toggles = {"categories": {}, "flavors": []}
    for cid, label, hint in DEFAULT_CATEGORY_TEMPLATES:
        categories.append(
            {
                "id": cid,
                "label": label,
                "enabled": True,
                "tree": _default_tree(cid, label, hint),
            }
        )
        toggles["categories"][cid] = True
    return {
        "version": 1,
        "categories": categories,
        "flavors": [],
        "toggles": toggles,
    }


def _normalize_node(node: Any, fallback_label: str = "Item") -> dict[str, Any]:
    if not isinstance(node, dict):
        node = {}
    label = str(node.get("label") or fallback_label).strip() or fallback_label
    normalized = {
        "id": str(node.get("id") or _gen_id("node")),
        "label": label,
        "weight": _safe_weight(node.get("weight", 1)),
        "flavors": _clean_str_list(node.get("flavors", [])),
        "children": [],
        "items": [],
    }
    for child in node.get("children", []):
        normalized["children"].append(_normalize_node(child, fallback_label=label))
    for item in node.get("items", []):
        normalized["items"].append(_normalize_node(item, fallback_label=label))
    return normalized


def _parse_tree_value(tree_value: Any, fallback_label: str) -> dict[str, Any]:
    if isinstance(tree_value, str):
        try:
            parsed = json.loads(tree_value)
            if isinstance(parsed, dict):
                return _normalize_node(parsed, fallback_label=fallback_label)
        except Exception:  # noqa: BLE001
            logger.error("invalid tree json for category %s", fallback_label)
        return _normalize_node({}, fallback_label=fallback_label)

    if isinstance(tree_value, dict):
        return _normalize_node(tree_value, fallback_label=fallback_label)

    return _normalize_node({}, fallback_label=fallback_label)


def _restore_template_keys(state: dict[str, Any]) -> dict[str, Any]:
    categories = state.get("categories")
    if isinstance(categories, list):
        for category in categories:
            if isinstance(category, dict):
                category.setdefault("__template_key", "category")

    flavors = state.get("flavors")
    if isinstance(flavors, list):
        for flavor in flavors:
            if isinstance(flavor, dict):
                flavor.setdefault("__template_key", "flavor")

    return state


def normalize_state(raw_state: Any) -> dict[str, Any]:
    base = default_state()
    if not isinstance(raw_state, dict):
        return base

    state = {
        "version": 1,
        "categories": [],
        "flavors": [],
        "toggles": {
            "categories": {},
            "flavors": [],
        },
    }

    raw_categories = raw_state.get("categories", [])
    if isinstance(raw_categories, list):
        for category in raw_categories:
            if not isinstance(category, dict):
                continue

            if category.get("__template_key") == "category":
                cid = str(category.get("id") or "").strip()
                if not cid:
                    continue
                label = str(category.get("label") or cid).strip() or cid
                state["categories"].append(
                    {
                        "id": cid,
                        "label": label,
                        "enabled": bool(category.get("enabled", True)),
                        "tree": _parse_tree_value(
                            category.get("tree"),
                            fallback_label=label,
                        ),
                    }
                )
                continue

            cid = str(category.get("id") or "").strip()
            if not cid:
                continue
            label = str(category.get("label") or cid).strip() or cid
            state["categories"].append(
                {
                    "id": cid,
                    "label": label,
                    "enabled": bool(category.get("enabled", True)),
                    "tree": _parse_tree_value(
                        category.get("tree", {}), fallback_label=label
                    ),
                }
            )
    elif isinstance(raw_categories, dict):
        for raw_cid, category in raw_categories.items():
            cid = str(raw_cid or "").strip()
            if not cid:
                continue
            if not isinstance(category, dict):
                category = {}
            label = str(category.get("label") or cid).strip() or cid
            tree_src = (
                category.get("tree")
                if isinstance(category.get("tree"), dict)
                else category
            )
            state["categories"].append(
                {
                    "id": cid,
                    "label": label,
                    "enabled": bool(category.get("enabled", True)),
                    "tree": _parse_tree_value(tree_src, fallback_label=label),
                }
            )

    if not state["categories"]:
        state["categories"] = base["categories"]

    raw_flavors = raw_state.get("flavors", [])
    if isinstance(raw_flavors, list):
        for flavor in raw_flavors:
            if not isinstance(flavor, dict):
                continue

            if flavor.get("__template_key") == "flavor":
                fid = str(flavor.get("id") or "").strip()
                label = str(flavor.get("label") or "").strip()
                if not fid or not label:
                    continue
                state["flavors"].append(
                    {
                        "id": fid,
                        "label": label,
                        "description": str(flavor.get("description") or "").strip(),
                        "enabled_by_default": bool(
                            flavor.get("enabled_by_default", False)
                        ),
                    }
                )
                continue

            fid = str(flavor.get("id") or "").strip()
            label = str(flavor.get("label") or "").strip()
            if not fid or not label:
                continue
            state["flavors"].append(
                {
                    "id": fid,
                    "label": label,
                    "description": str(flavor.get("description") or "").strip(),
                    "enabled_by_default": bool(flavor.get("enabled_by_default", False)),
                }
            )
    elif isinstance(raw_flavors, dict):
        for raw_fid, flavor in raw_flavors.items():
            fid = str(raw_fid or "").strip()
            if not fid:
                continue
            if isinstance(flavor, str):
                label = flavor.strip()
                if not label:
                    continue
                state["flavors"].append(
                    {
                        "id": fid,
                        "label": label,
                        "description": "",
                        "enabled_by_default": False,
                    }
                )
                continue
            if not isinstance(flavor, dict):
                continue
            label = str(flavor.get("label") or "").strip()
            if not label:
                continue
            state["flavors"].append(
                {
                    "id": fid,
                    "label": label,
                    "description": str(flavor.get("description") or "").strip(),
                    "enabled_by_default": bool(flavor.get("enabled_by_default", False)),
                }
            )

    raw_toggles = (
        raw_state.get("toggles", {})
        if isinstance(raw_state.get("toggles"), dict)
        else {}
    )
    raw_category_toggles = (
        raw_toggles.get("categories")
        if isinstance(raw_toggles.get("categories"), dict)
        else {}
    )
    for category in state["categories"]:
        cid = category["id"]
        state["toggles"]["categories"][cid] = bool(
            raw_category_toggles.get(cid, category.get("enabled", True)),
        )

    flavor_ids = {flavor["id"] for flavor in state["flavors"]}
    for fid in _clean_str_list(raw_toggles.get("flavors", [])):
        if fid in flavor_ids:
            state["toggles"]["flavors"].append(fid)

    return _restore_template_keys(state)


def _first_level_candidates(node: FoodNode) -> list[FoodNode]:
    return node.children if node.children else node.items


def _filter_by_flavors(
    nodes: list[FoodNode], flavor_filter: set[str]
) -> list[FoodNode]:
    if not flavor_filter:
        return nodes
    filtered = [
        node
        for node in nodes
        if not node.flavors or bool(set(node.flavors) & flavor_filter)
    ]
    return filtered or nodes


def _pick_path(root: FoodNode, flavor_filter: set[str]) -> list[str]:
    path = [root.label]
    current = root
    while True:
        candidates = _filter_by_flavors(_first_level_candidates(current), flavor_filter)
        if not candidates:
            break
        weights = [node.weight for node in candidates]
        chosen = random.choices(candidates, weights=weights, k=1)[0]
        path.append(chosen.label)
        current = chosen
    return path


def _find_node_by_id(node: dict[str, Any], target_id: str) -> dict[str, Any] | None:
    if node.get("id") == target_id:
        return node
    for child in node.get("children", []):
        found = _find_node_by_id(child, target_id)
        if found:
            return found
    for item in node.get("items", []):
        found = _find_node_by_id(item, target_id)
        if found:
            return found
    return None


@register(
    "what2eat",
    "KUR0TO",
    "Dinner picker with multi-tree and flavor-aware AI suggestions",
    "1.2.0",
)
class What2EatPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._data_dir: Path | None = None
        self._state_path: Path | None = None
        self._state_lock = asyncio.Lock()

    async def initialize(self):
        StarTools.initialize(self.context)
        self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._state_path = self._data_dir / STORAGE_FILENAME
        await self._load_and_migrate_state()
        self._register_web_apis()

    async def terminate(self):
        await self._save_config()

    async def _load_and_migrate_state(self) -> None:
        if not self._state_path:
            raise RuntimeError("state path is not initialized")

        default_cfg = default_state()
        current_cfg = normalize_state(dict(self.config))
        old_state = None

        if self._state_path.exists():
            try:
                raw_state = json.loads(self._state_path.read_text("utf-8"))
                old_state = normalize_state(raw_state)
            except Exception as exc:  # noqa: BLE001
                logger.error("failed to load legacy what2eat state: %s", exc)

        # Migrate once: only overwrite plugin config when config is still default.
        if old_state is not None and _deep_equal(current_cfg, default_cfg):
            self.config.save_config(old_state)
            logger.info(
                "what2eat legacy state migrated to plugin config, legacy file kept at %s",
                self._state_path,
            )
            current_cfg = old_state

        normalized_cfg = _restore_template_keys(normalize_state(current_cfg))
        if not _deep_equal(normalized_cfg, dict(self.config)):
            self.config.save_config(normalized_cfg)
            logger.info("what2eat config normalized and persisted")

    async def _save_config(self) -> None:
        async with self._state_lock:
            self.config.save_config()

    def _state(self) -> dict[str, Any]:
        return _restore_template_keys(normalize_state(dict(self.config)))

    def _register_web_apis(self) -> None:
        self.context.register_web_api(
            route="/what2eat/state",
            view_handler=self.api_get_state,
            methods=["GET"],
            desc="Get what2eat plugin state",
        )
        self.context.register_web_api(
            route="/what2eat/state",
            view_handler=self.api_update_state,
            methods=["POST"],
            desc="Update what2eat plugin state",
        )
        self.context.register_web_api(
            route="/what2eat/toggles",
            view_handler=self.api_update_toggles,
            methods=["POST"],
            desc="Update what2eat toggles",
        )
        self.context.register_web_api(
            route="/what2eat/pick",
            view_handler=self.api_pick_preview,
            methods=["POST"],
            desc="Preview one meal result",
        )

    def _current_flavor_filter(self) -> set[str]:
        return set(self._state().get("toggles", {}).get("flavors", []))

    def _build_pick_result(
        self, flavor_filter: set[str] | None = None
    ) -> list[dict[str, Any]]:
        state = self._state()
        if flavor_filter is None:
            flavor_filter = self._current_flavor_filter()
        toggles = state.get("toggles", {}).get("categories", {})

        picked: list[dict[str, Any]] = []
        for category in state.get("categories", []):
            category_id = category.get("id", "")
            if not toggles.get(category_id, True):
                continue
            try:
                root = FoodNode.from_dict(category.get("tree", {}))
                path = _pick_path(root, flavor_filter)
                picked.append(
                    {
                        "category_id": category_id,
                        "category_label": category.get("label", category_id),
                        "path": path,
                        "result": path[-1] if path else "",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("failed to pick category %s: %s", category_id, exc)
        return picked

    @event_filter.command("what2eat")
    async def command_what2eat(self, event: AstrMessageEvent):
        state = self._state()
        picked = self._build_pick_result()
        if not picked:
            yield event.plain_result(
                "No enabled category can be picked. Configure categories in plugin panel."
            )
            return
        lines = [
            f"{item['category_label']}: {' -> '.join(item['path'])}" for item in picked
        ]
        active_flavors = state.get("toggles", {}).get("flavors", [])
        if active_flavors:
            lines.append(f"Flavors: {', '.join(active_flavors)}")
        yield event.plain_result("\n".join(lines))

    @event_filter.command("what2eat_flavors")
    async def command_what2eat_flavors(self, event: AstrMessageEvent):
        state = self._state()
        flavors = state.get("flavors", [])
        if not flavors:
            yield event.plain_result("No flavors configured.")
            return
        active = set(state.get("toggles", {}).get("flavors", []))
        lines = [
            f"{'[x]' if flavor['id'] in active else '[ ]'} {flavor['id']} ({flavor['label']})"
            for flavor in flavors
        ]
        yield event.plain_result("\n".join(lines))

    async def api_get_state(self):
        return _ok(self._state())

    async def api_update_state(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("invalid payload")
        new_state = _restore_template_keys(normalize_state(payload.get("state", payload)))
        self.config.save_config(new_state)
        return _ok(self._state(), "saved")

    async def api_update_toggles(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("invalid payload")

        toggles = payload.get("toggles")
        if not isinstance(toggles, dict):
            return _error("toggles must be an object")

        state = self._state()

        category_ids = {cat["id"] for cat in state.get("categories", [])}
        flavor_ids = {flavor["id"] for flavor in state.get("flavors", [])}

        raw_categories = toggles.get("categories", {})
        if isinstance(raw_categories, dict):
            for cid in category_ids:
                state["toggles"]["categories"][cid] = bool(
                    raw_categories.get(cid, True)
                )

        raw_flavors = _clean_str_list(toggles.get("flavors", []))
        state["toggles"]["flavors"] = [fid for fid in raw_flavors if fid in flavor_ids]

        self.config.save_config(_restore_template_keys(state))
        return _ok(state["toggles"], "toggles updated")

    async def api_pick_preview(self):
        payload = await request.get_json(silent=True)
        flavor_filter = self._current_flavor_filter()
        if isinstance(payload, dict):
            flavor_filter = (
                set(_clean_str_list(payload.get("flavors", []))) or flavor_filter
            )
        picked = self._build_pick_result(flavor_filter=flavor_filter)
        return _ok({"result": picked})
