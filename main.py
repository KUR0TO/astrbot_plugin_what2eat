from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quart import jsonify, request

from astrbot.api import logger
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


@dataclass
class FoodNode:
    id: str
    label: str
    weight: int = 1
    flavors: list[str] = field(default_factory=list)
    children: list["FoodNode"] = field(default_factory=list)
    items: list["FoodNode"] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FoodNode":
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


def _default_tree(label: str, hint: str) -> dict[str, Any]:
    return {
        "id": _gen_id("root"),
        "label": label,
        "weight": 1,
        "flavors": [],
        "children": [],
        "items": [
            {
                "id": _gen_id("item"),
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
                "tree": _default_tree(label, hint),
            }
        )
        toggles["categories"][cid] = True
    return {
        "version": 1,
        "provider_id": "",
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


def normalize_state(raw_state: Any) -> dict[str, Any]:
    base = default_state()
    if not isinstance(raw_state, dict):
        return base

    state = {
        "version": 1,
        "provider_id": str(raw_state.get("provider_id") or "").strip(),
        "categories": [],
        "flavors": [],
        "toggles": {
            "categories": {},
            "flavors": [],
        },
    }

    for category in raw_state.get("categories", []):
        if not isinstance(category, dict):
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
                "tree": _normalize_node(category.get("tree", {}), fallback_label=label),
            }
        )

    if not state["categories"]:
        state["categories"] = base["categories"]

    for flavor in raw_state.get("flavors", []):
        if not isinstance(flavor, dict):
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

    category_ids = {cat["id"] for cat in state["categories"]}
    raw_toggles = raw_state.get("toggles", {}) if isinstance(raw_state.get("toggles"), dict) else {}
    raw_category_toggles = (
        raw_toggles.get("categories") if isinstance(raw_toggles.get("categories"), dict) else {}
    )
    for cid in category_ids:
        state["toggles"]["categories"][cid] = bool(raw_category_toggles.get(cid, True))

    flavor_ids = {flavor["id"] for flavor in state["flavors"]}
    for fid in _clean_str_list(raw_toggles.get("flavors", [])):
        if fid in flavor_ids:
            state["toggles"]["flavors"].append(fid)

    return state


def _extract_json_payload(text: str) -> Any:
    text = text.strip()
    if not text:
        raise ValueError("empty response")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if not match:
            raise
        return json.loads(match.group(1))


def _first_level_candidates(node: FoodNode) -> list[FoodNode]:
    return node.children if node.children else node.items


def _filter_by_flavors(nodes: list[FoodNode], flavor_filter: set[str]) -> list[FoodNode]:
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
    def __init__(self, context: Context):
        super().__init__(context)
        self._data_dir: Path | None = None
        self._state_path: Path | None = None
        self._state: dict[str, Any] = {}
        self._state_lock = asyncio.Lock()

    async def initialize(self):
        StarTools.initialize(self.context)
        self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._state_path = self._data_dir / STORAGE_FILENAME
        await self._load_state()
        self._register_web_apis()

    async def terminate(self):
        await self._save_state()

    async def _load_state(self) -> None:
        if not self._state_path:
            raise RuntimeError("state path is not initialized")
        if self._state_path.exists():
            try:
                raw_state = json.loads(self._state_path.read_text("utf-8"))
                self._state = normalize_state(raw_state)
                return
            except Exception as exc:  # noqa: BLE001
                logger.error("failed to load what2eat state, using default: %s", exc)
        self._state = default_state()
        await self._save_state()

    async def _save_state(self) -> None:
        if not self._state_path:
            raise RuntimeError("state path is not initialized")
        async with self._state_lock:
            self._state_path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

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
            route="/what2eat/providers",
            view_handler=self.api_get_providers,
            methods=["GET"],
            desc="Get provider options",
        )
        self.context.register_web_api(
            route="/what2eat/pick",
            view_handler=self.api_pick_preview,
            methods=["POST"],
            desc="Preview one meal result",
        )
        self.context.register_web_api(
            route="/what2eat/ai_suggest",
            view_handler=self.api_ai_suggest,
            methods=["POST"],
            desc="Generate and append AI suggestions",
        )

    def _current_flavor_filter(self) -> set[str]:
        return set(self._state.get("toggles", {}).get("flavors", []))

    def _build_pick_result(self, flavor_filter: set[str] | None = None) -> list[dict[str, Any]]:
        if flavor_filter is None:
            flavor_filter = self._current_flavor_filter()
        toggles = self._state.get("toggles", {}).get("categories", {})

        picked: list[dict[str, Any]] = []
        for category in self._state.get("categories", []):
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
        picked = self._build_pick_result()
        if not picked:
            yield event.plain_result("No enabled category can be picked. Configure categories in plugin panel.")
            return
        lines = [f"{item['category_label']}: {' -> '.join(item['path'])}" for item in picked]
        active_flavors = self._state.get("toggles", {}).get("flavors", [])
        if active_flavors:
            lines.append(f"Flavors: {', '.join(active_flavors)}")
        yield event.plain_result("\n".join(lines))

    @event_filter.command("what2eat_flavors")
    async def command_what2eat_flavors(self, event: AstrMessageEvent):
        flavors = self._state.get("flavors", [])
        if not flavors:
            yield event.plain_result("No flavors configured.")
            return
        active = set(self._state.get("toggles", {}).get("flavors", []))
        lines = [
            f"{'[x]' if flavor['id'] in active else '[ ]'} {flavor['id']} ({flavor['label']})"
            for flavor in flavors
        ]
        yield event.plain_result("\n".join(lines))

    async def api_get_state(self):
        return _ok(self._state)

    async def api_update_state(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("invalid payload")
        new_state = normalize_state(payload.get("state", payload))
        self._state = new_state
        await self._save_state()
        return _ok(self._state, "saved")

    async def api_update_toggles(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("invalid payload")

        toggles = payload.get("toggles")
        if not isinstance(toggles, dict):
            return _error("toggles must be an object")

        category_ids = {cat["id"] for cat in self._state.get("categories", [])}
        flavor_ids = {flavor["id"] for flavor in self._state.get("flavors", [])}

        raw_categories = toggles.get("categories", {})
        if isinstance(raw_categories, dict):
            for cid in category_ids:
                self._state["toggles"]["categories"][cid] = bool(raw_categories.get(cid, True))

        raw_flavors = _clean_str_list(toggles.get("flavors", []))
        self._state["toggles"]["flavors"] = [fid for fid in raw_flavors if fid in flavor_ids]

        await self._save_state()
        return _ok(self._state["toggles"], "toggles updated")

    async def api_get_providers(self):
        providers = []
        for provider in self.context.get_all_providers():
            meta = provider.meta()
            providers.append(
                {
                    "id": meta.id,
                    "model": meta.model,
                    "type": meta.type,
                }
            )
        return _ok(providers)

    async def api_pick_preview(self):
        payload = await request.get_json(silent=True)
        flavor_filter = self._current_flavor_filter()
        if isinstance(payload, dict):
            flavor_filter = set(_clean_str_list(payload.get("flavors", []))) or flavor_filter
        picked = self._build_pick_result(flavor_filter=flavor_filter)
        return _ok({"result": picked})

    async def api_ai_suggest(self):
        payload = await request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("invalid payload")

        category_id = str(payload.get("category_id") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        flavor_ids = _clean_str_list(payload.get("flavor_ids", []))
        count = max(1, min(30, int(payload.get("count", 8))))
        prompt_hint = str(payload.get("prompt_hint") or "").strip()

        provider_id = str(payload.get("provider_id") or self._state.get("provider_id") or "").strip()
        if not provider_id:
            return _error("provider_id is required")

        category = next(
            (cat for cat in self._state.get("categories", []) if cat.get("id") == category_id),
            None,
        )
        if not category:
            return _error("category not found")

        root = category.get("tree", {})
        target_node = _find_node_by_id(root, node_id) if node_id else root
        if not isinstance(target_node, dict):
            return _error("target node not found")

        prompt = (
            "You are generating dinner candidates for a food picker tree. "
            "Return strict JSON only with shape: "
            '{"items":["name1","name2"],"nodes":[{"label":"group","items":["a","b"]}]}.'
            f"\nCategory: {category.get('label')}"
            f"\nFlavor IDs: {', '.join(flavor_ids) if flavor_ids else 'none'}"
            f"\nNeed count: {count}"
            f"\nCurrent node label: {target_node.get('label', '')}"
            f"\nExtra hint: {prompt_hint or 'none'}"
        )

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("what2eat ai generation failed: %s", exc)
            return _error(f"ai call failed: {exc}")

        raw_text = llm_resp.completion_text or ""
        try:
            parsed = _extract_json_payload(raw_text)
        except Exception as exc:  # noqa: BLE001
            logger.error("what2eat ai response parse failed: %s", exc)
            return _error("ai returned invalid json")

        appended: list[str] = []
        if isinstance(parsed, dict):
            for name in _clean_str_list(parsed.get("items", []))[:count]:
                target_node.setdefault("items", []).append(
                    _normalize_node(
                        {
                            "id": _gen_id("item"),
                            "label": name,
                            "flavors": flavor_ids,
                            "weight": 1,
                            "children": [],
                            "items": [],
                        },
                        fallback_label=name,
                    )
                )
                appended.append(name)

            for node in parsed.get("nodes", []):
                normalized = _normalize_node(node)
                if flavor_ids and not normalized.get("flavors"):
                    normalized["flavors"] = flavor_ids
                target_node.setdefault("children", []).append(normalized)
                appended.append(normalized["label"])
        elif isinstance(parsed, list):
            for name in _clean_str_list(parsed)[:count]:
                target_node.setdefault("items", []).append(
                    _normalize_node(
                        {
                            "id": _gen_id("item"),
                            "label": name,
                            "flavors": flavor_ids,
                            "weight": 1,
                            "children": [],
                            "items": [],
                        },
                        fallback_label=name,
                    )
                )
                appended.append(name)

        if not appended:
            return _error("ai response did not contain usable items")

        self._state["provider_id"] = provider_id
        await self._save_state()

        return _ok(
            {
                "appended": appended,
                "raw_text": raw_text,
                "state": self._state,
            },
            "ai suggestions appended",
        )
