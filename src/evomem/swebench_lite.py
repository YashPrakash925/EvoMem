"""SWE-bench Lite dataset adapter and official-harness integration."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DATASET_NAME = "SWE-bench/SWE-bench_Lite"


def load_lite_instances(*, dataset_name: str = DATASET_NAME, split: str = "test") -> list[dict[str, Any]]:
    """Load public issue fields only; gold patches and test labels are omitted."""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError("SWE-bench planning requires the optional datasets package; install with `pip install -e '.[benchmark]'`.") from exc
    dataset = load_dataset(dataset_name, split=split)
    rows: list[dict[str, Any]] = []
    for row in dataset:
        rows.append({
            "instance_id": str(row["instance_id"]),
            "repo": str(row["repo"]),
            "base_commit": str(row["base_commit"]),
            "problem_statement": str(row["problem_statement"]),
            "image": str(row["image"]) if row.get("image") else None,
        })
    return rows


def write_plan(output_dir: str | Path, *, seed: int = 7, model: str = "unspecified", max_steps: int = 50,
               instance_ids: list[str] | None = None) -> dict[str, Any]:
    """Freeze an exposure order and the existing five-condition evaluation plan."""
    from .evaluation_runner import EvaluationTask, build_plan, write_manifest

    rows = load_lite_instances()
    if instance_ids:
        wanted = set(instance_ids)
        known = {row["instance_id"] for row in rows}
        unknown = wanted - known
        if unknown:
            raise ValueError(f"Unknown SWE-bench Lite instance IDs: {sorted(unknown)}")
        rows = [row for row in rows if row["instance_id"] in wanted]
    if not rows:
        raise ValueError("SWE-bench Lite selection is empty.")

    rng = random.Random(seed)
    repos: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        repos.setdefault(row["repo"], []).append(row)
    ordered: list[dict[str, Any]] = []
    for repo in sorted(repos):
        group = repos[repo]
        rng.shuffle(group)
        ordered.extend(group)
    task_rows: list[dict[str, Any]] = []
    tasks: list[EvaluationTask] = []
    for order, row in enumerate(ordered):
        task_rows.append({"task_id": row["instance_id"], "repository": row["repo"],
                          "order": order, "language": "Python", "base_commit": row["base_commit"],
                          "task_prompt": row["problem_statement"], "image": row.get("image")})
        tasks.append(EvaluationTask(task_id=row["instance_id"], repository=row["repo"],
                                    order=order, language="Python", base_commit=row["base_commit"],
                                    task_prompt=row["problem_statement"], image=row.get("image")))

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    task_path = root / "tasks.json"
    task_path.write_text(json.dumps({"dataset": DATASET_NAME, "split": "test", "seed": seed,
                                     "tasks": task_rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = write_manifest(build_plan(tasks), root / "run-manifest.json", agent="evomem-openhands",
                              model=model, seed=seed, budget={"max_steps": max_steps})
    return {"dataset": DATASET_NAME, "split": "test", "task_count": len(task_rows),
            "planned_runs": len(manifest["runs"]), "tasks_path": str(task_path),
            "manifest_path": str(root / "run-manifest.json"), "seed": seed,
            "gold_labels_included": False}


def export_predictions(run_root: str | Path, output: str | Path, *, condition: str, model: str) -> dict[str, Any]:
    """Collect per-instance model_patch.diff files into the official JSONL format."""
    root = Path(run_root)
    if not root.is_dir():
        raise ValueError(f"Run root does not exist: {root}")
    predictions = []
    for patch_path in sorted(root.glob(f"*/{condition}/model_patch.diff")):
        instance_id = patch_path.parent.parent.name
        predictions.append({"instance_id": instance_id, "model_name_or_path": model,
                            "model_patch": patch_path.read_text(encoding="utf-8")})
    if not predictions:
        raise ValueError(f"No */{condition}/model_patch.diff files found under {root}")
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in predictions), encoding="utf-8")
    return {"status": "written", "count": len(predictions), "condition": condition,
            "output": str(destination)}


def _harness_dataset(predictions: str | Path) -> str:
    """Use a local dataset copy with Unix newlines for Windows Docker grading."""
    if sys.platform != "win32":
        return DATASET_NAME
    try:
        from datasets import DownloadConfig, load_dataset
    except ImportError as exc:
        raise RuntimeError("SWE-bench grading requires the optional datasets package.") from exc
    dataset = load_dataset(DATASET_NAME, split="test", download_config=DownloadConfig(local_files_only=True))
    destination = Path(predictions).resolve().parent / "swebench-lite-test.jsonl"
    with destination.open("w", encoding="utf-8", newline="\n") as stream:
        for source in dataset:
            row = dict(source)
            script = row.get("eval_script")
            if isinstance(script, str):
                row["eval_script"] = script.replace("\r\n", "\n").replace("\r", "\n")
            stream.write(json.dumps(row, ensure_ascii=True) + "\n")
    return str(destination)


def grade_predictions(predictions: str | Path, *, run_id: str, workers: int = 1,
                      instance_ids: list[str] | None = None) -> int:
    """Delegate correctness grading to the official SWE-bench Docker harness."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if not Path(predictions).is_file():
        raise ValueError(f"Prediction file does not exist: {predictions}")
    dataset_name = _harness_dataset(predictions)
    if sys.platform == "win32":
        bootstrap = (
            "import pathlib,runpy; original=pathlib.Path.write_text; "
            "pathlib.Path.write_text=lambda self,data,*a,**k: original(self,data,*a,**({**k,'newline':'\\n'} if self.name=='eval.sh' else k)); "
            "runpy.run_module('swebench.harness.run_evaluation',run_name='__main__')"
        )
        command = [sys.executable, "-c", bootstrap, "--dataset_name", dataset_name,
                   "--split", "test", "--predictions_path", str(predictions),
                   "--run_id", run_id, "--max_workers", str(workers)]
    elif shutil.which("swebench"):
        command = ["swebench", "eval", dataset_name, "-p", str(predictions), "--run-id", run_id, "-j", str(workers)]
    else:
        command = [sys.executable, "-m", "swebench.harness.run_evaluation", "--dataset_name", dataset_name,
                   "--split", "test", "--predictions_path", str(predictions), "--run_id", run_id,
                   "--max_workers", str(workers)]
    if instance_ids:
        command.extend(["-i", *instance_ids])
    completed = subprocess.run(command, check=False)
    return int(completed.returncode)
