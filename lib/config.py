"""Chargement des configurations clients depuis configs/clients/."""

import json
import os
from pathlib import Path
from typing import Optional


CONFIG_DIR = Path(__file__).parent.parent / "configs" / "clients"
PROMPTS_DIR = Path(__file__).parent.parent / "configs" / "prompts"

# Cache en mémoire
_configs: dict[str, dict] = {}


def load_all_configs() -> dict[str, dict]:
    """Charge toutes les configs clients. Clé = workspace_id."""
    global _configs
    _configs = {}
    for file in CONFIG_DIR.glob("*.json"):
        if file.name.startswith("_"):
            continue
        with open(file) as f:
            config = json.load(f)
        ws_id = config.get("workspace_id")
        if ws_id:
            _configs[ws_id] = config
    return _configs


def get_config(workspace_id: str) -> Optional[dict]:
    """Récupère la config d'un client par workspace_id."""
    if not _configs:
        load_all_configs()
    return _configs.get(workspace_id)


def get_prompt(name: str) -> str:
    """Charge un prompt depuis configs/prompts/."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt introuvable: {path}")
    return path.read_text()
