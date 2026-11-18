"""Export completed agent runs as reviewable, untrusted candidates."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ingestion import normalize


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Run is missing required {label}: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed {label} JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def export_run(run_dir: str | Path, output: str | Path | None = None) -> dict[str, Any]:
    """Write an untrusted review bundle from request, trajectory, and outcome artifacts.

    This function deliberately does not validate claims or access a memory store.
    """
    root = Path(run_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"Run directory does not exist: {root}")
    request = _read_object(root / "request.json", "request")
    outcome = _read_object(root / "outcome.json", "outcome")
    trajectory_path = root / "trajectory.json"
    if not trajectory_path.is_file():
        raise ValueError("Run is missing required trajectory: trajectory.json")
    trajectory = normalize(trajectory_path).to_dict()
    status = str(outcome.get("status", "unknown")).lower()
    review_eligible = status == "completed" and bool(trajectory["events"])
    bundle = {
        "schema_version": "1.0",
        "candidate_status": "candidate",
        "review_eligible": review_eligible,
        "review_note": ("Run artifacts are available for independent review; no claims have been validated."
                        if review_eligible else "Run is incomplete or has no usable events; inspect before review."),
        "provenance": {
            "run_directory": str(root),
            "task_id": request.get("task_id"),
            "condition": request.get("condition"),
            "repository": request.get("repository"),
            "repository_path": request.get("repository_path"),
            "manifest_agent": request.get("manifest_agent"),
            "manifest_model": request.get("manifest_model"),
        },
        "task": request.get("task_prompt", request.get("task")),
        "outcome": outcome,
        "agent_context": request.get("agent_context", {}),
        "trajectory": trajectory,
    }
    destination = Path(output) if output else root / "review-candidate.json"
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "exported", "review_eligible": review_eligible, "output": str(destination),
            "candidate_status": "candidate"}
