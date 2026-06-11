from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def get_repo_state(self, repo: str) -> dict[str, Any]:
        state = self.load()
        repos = state.get("repos") if isinstance(state.get("repos"), dict) else {}
        repo_state = repos.get(repo) if isinstance(repos.get(repo), dict) else {}
        return dict(repo_state)

    def update_repo_state(self, repo: str, repo_state: dict[str, Any]) -> None:
        state = self.load()
        repos = state.get("repos") if isinstance(state.get("repos"), dict) else {}
        repos[repo] = dict(repo_state)
        state["repos"] = repos
        self.save(state)

