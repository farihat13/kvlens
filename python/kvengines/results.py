"""Structured result saving and loading.

Directory layout:
    base_dir/<db>/sf<sf>/<query_id>/p<param_idx>/
        raw.json          — untouched DB response
        normalized.json   — parsed into LDBC schema format (if available)
        metadata.json     — db, sf, query, param_idx, latency_ms, timestamp, etc.
        hooks.json        — before/after hook snapshots (if any)
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _serialize(obj: Any) -> Any:
    """Make an object JSON-serializable."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, list):
        return [_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


class ResultStore:
    """Save and load query results in a structured directory layout.

    Args:
        base_dir: Root directory for all results.
    """

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)

    def _result_dir(self, db: str, sf: str, query_id: str, param_idx: int) -> Path:
        return self.base_dir / db / f"sf{sf}" / query_id / f"p{param_idx}"

    def save(
        self,
        db: str,
        sf: str,
        query_id: str,
        param_idx: int,
        raw: Any,
        normalized: Any | None = None,
        latency_ms: float | None = None,
        metadata: dict | None = None,
        hook_data: dict | None = None,
    ) -> Path:
        """Save a query result to the structured directory layout.

        Returns:
            Path to the result directory.
        """
        d = self._result_dir(db, sf, query_id, param_idx)
        d.mkdir(parents=True, exist_ok=True)

        # Raw response
        (d / "raw.json").write_text(
            json.dumps(_serialize(raw), indent=2, default=str), encoding="utf-8"
        )

        # Normalized (LDBC schema) results
        if normalized is not None:
            (d / "normalized.json").write_text(
                json.dumps(_serialize(normalized), indent=2, default=str), encoding="utf-8"
            )

        # Metadata
        meta = {
            "db": db,
            "sf": sf,
            "query_id": query_id,
            "param_idx": param_idx,
            "latency_ms": latency_ms,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            **(metadata or {}),
        }
        (d / "metadata.json").write_text(
            json.dumps(meta, indent=2, default=str), encoding="utf-8"
        )

        # Hook snapshots
        if hook_data:
            (d / "hooks.json").write_text(
                json.dumps(_serialize(hook_data), indent=2, default=str), encoding="utf-8"
            )

        return d

    def load(self, db: str, sf: str, query_id: str, param_idx: int) -> dict[str, Any]:
        """Load a previously saved result.

        Returns:
            Dict with keys: raw, normalized (or None), metadata, hooks (or None).

        Raises:
            FileNotFoundError: If the result directory does not exist.
        """
        d = self._result_dir(db, sf, query_id, param_idx)
        if not d.exists():
            raise FileNotFoundError(f"No results at {d}")

        result: dict[str, Any] = {}
        result["raw"] = json.loads((d / "raw.json").read_text(encoding="utf-8"))
        result["metadata"] = json.loads((d / "metadata.json").read_text(encoding="utf-8"))

        norm_path = d / "normalized.json"
        result["normalized"] = (
            json.loads(norm_path.read_text(encoding="utf-8")) if norm_path.exists() else None
        )

        hooks_path = d / "hooks.json"
        result["hooks"] = (
            json.loads(hooks_path.read_text(encoding="utf-8")) if hooks_path.exists() else None
        )

        return result

    def list(
        self, db: str | None = None, sf: str | None = None
    ) -> list[Path]:
        """List result directories, optionally filtered by db and/or scale factor."""
        if not self.base_dir.exists():
            return []

        if db and sf:
            root = self.base_dir / db / f"sf{sf}"
        elif db:
            root = self.base_dir / db
        else:
            root = self.base_dir

        if not root.exists():
            return []

        # Find leaf directories that contain a raw.json
        return sorted(p.parent for p in root.rglob("raw.json"))
