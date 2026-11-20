"""The model catalog the tool maintains for Codex (`model_catalog_json`).

Codex only sends the `apply_patch` tool to models it has a catalog record for;
any other model name gets a built-in fallback record without it. The tool keeps
one record per model name used in its API presets in models.json inside Codex's
own directory (next to config.toml, where DeepSeek's Codex guide puts it), and
points Codex's config.toml at that file while the proxy is on.

Each record repeats Codex's own fallback record (codex-rs/models-manager/src/
model_info.rs, `model_info_from_slug`, Codex 0.167.0) with two differences:
`apply_patch_tool_type` is "freeform" and `visibility` is "list". Records are
only ever added: existing ones are never changed or removed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CATALOG_FILE_NAME = "models.json"  # lives in Codex's directory, next to config.toml
CONFIG_KEY = "model_catalog_json"  # top-level setting in Codex's config.toml
# Codex's built-in base instructions (prompt.md of Codex 0.167.0), byte for byte
# as compiled into the Windows build, i.e. what the fallback record uses.
BASE_INSTRUCTIONS_FILE = Path(__file__).with_name("codex_base_instructions.md")


class CatalogError(Exception):
    pass


def base_instructions() -> str:
    try:
        return BASE_INSTRUCTIONS_FILE.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CatalogError(f"无法读取 cct/{BASE_INSTRUCTIONS_FILE.name}：{exc}") from exc


def model_entry(slug: str, instructions: str) -> dict[str, Any]:
    """Catalog record for `slug`: Codex's fallback record plus the apply_patch tool."""
    return {
        "slug": slug,
        "display_name": slug,
        "description": None,
        "default_reasoning_level": None,
        "supported_reasoning_levels": [],
        "shell_type": "unified_exec",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 99,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "default_service_tier": None,
        "availability_nux": None,
        "upgrade": None,
        "model_messages": {"instructions_template": instructions},
        "include_skills_usage_instructions": False,
        "include_plugin_usage_instructions": False,
        "include_apps_usage_instructions": False,
        "supports_reasoning_summary_parameter": True,
        "default_reasoning_summary": "auto",
        "support_verbosity": False,
        "default_verbosity": None,
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "supports_image_detail_original": False,
        "context_window": 272000,
        "max_context_window": 272000,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_search_tool": False,
    }


def preset_models(proxy: dict[str, Any]) -> list[str]:
    """Model names of all API presets, in order, without blanks and repeats."""
    return list(dict.fromkeys(preset["model"] for preset in proxy["presets"] if preset["model"]))


def sync(path: Path, models: list[str]) -> tuple[list[str], int]:
    """Add a record for every name in `models` the file lacks.

    Returns (names added, number of records now in the file). The file is only
    written when something is added; a file that cannot be understood is left
    alone and reported with CatalogError.
    """
    if path.exists():
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise CatalogError(f"无法读取 {path.name}：{exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise CatalogError(f'{path.name} 的内容不是 {{"models": [...]}} 的形式')
    else:
        data = {"models": []}
    known = {entry.get("slug") for entry in data["models"] if isinstance(entry, dict)}
    added = [slug for slug in models if slug not in known]
    if added:
        instructions = base_instructions()
        data["models"] += [model_entry(slug, instructions) for slug in added]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            os.replace(tmp, path)
        except OSError as exc:
            raise CatalogError(f"无法写入 {path.name}：{exc}") from exc
    return added, len(data["models"])
