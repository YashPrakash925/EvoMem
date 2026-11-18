from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from .distillation import deterministic_distill, distill_with_provider
from .gating import gate
from .ingestion import normalize
from .repository import MemoryRepository
from .retrieval import retrieve
from .schemas import memory_from_dict
from .validation import validate
from .indexing import rebuild
from .evaluation_runner import build_plan, load_and_validate_records, load_tasks, write_manifest, write_report
from .repository_context import collect
from .execution import DockerExecutor, execute_if_isolated
from .providers import GeminiREST
from .integration import memory_context
from .openhands_adapter import health as openhands_health
from .openhands_runner import execute_prepared, execute_sandboxed, prepare_issue_run, prepare_run
from .trajectory_export import export_run
from .evaluation_runner import CONDITIONS
from .swebench_lite import write_plan as write_swebench_plan, export_predictions as export_swebench_predictions, grade_predictions as grade_swebench_predictions


def dump(value: object) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    print(json.dumps(value, indent=2, default=lambda item: item.__dict__))


def main() -> None:
    parser = argparse.ArgumentParser(prog="evomem", description="Evidence-grounded experience memory prototype")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest"); ingest.add_argument("--trajectory", required=True); ingest.add_argument("--output")
    pipeline = sub.add_parser("pipeline"); pipeline.add_argument("--trajectory", required=True); pipeline.add_argument("--store", default="data/memories"); pipeline.add_argument("--snapshot-root"); pipeline.add_argument("--provider", choices=("local", "gemini"), default="local"); pipeline.add_argument("--model", default="gemini-3.8-flash"); pipeline.add_argument("--task-id", help="Attach the originating task ID for leakage-safe benchmark filtering"); pipeline.add_argument("--candidate-only", action="store_true", help="Save an unvalidated candidate; use only an isolated candidate store")
    retrieve_cmd = sub.add_parser("retrieve"); retrieve_cmd.add_argument("--query", required=True); retrieve_cmd.add_argument("--store", default="data/memories"); retrieve_cmd.add_argument("--language"); retrieve_cmd.add_argument("--top-k", type=int, default=5)
    index_cmd = sub.add_parser("index"); index_cmd.add_argument("--store", default="data/memories"); index_cmd.add_argument("--output", default="artifacts/indexes/memory-index.json"); index_cmd.add_argument("--provider", choices=("local", "gemini"), default="local"); index_cmd.add_argument("--model", default="gemini-embedding-001")
    health = sub.add_parser("health"); health.add_argument("--store", default="data/memories")
    evaluation = sub.add_parser("evaluate"); evaluation.add_argument("--output", default="artifacts/reports/evaluation.json"); evaluation.add_argument("--dataset"); evaluation.add_argument("--results"); evaluation.add_argument("--manifest"); evaluation.add_argument("--agent", default="external-adapter"); evaluation.add_argument("--agent-model", default="unspecified"); evaluation.add_argument("--seed", type=int); evaluation.add_argument("--max-steps", type=int, default=50)
    agent_context = sub.add_parser("agent-context"); agent_context.add_argument("--task", required=True); agent_context.add_argument("--task-id"); agent_context.add_argument("--store", default="data/memories"); agent_context.add_argument("--language"); agent_context.add_argument("--context-json")
    issue_run = sub.add_parser("issue-run", help="Prepare or execute the standard retrieval-gated issue workflow")
    issue_run.add_argument("--task", required=True, help="Issue description or task prompt")
    issue_run.add_argument("--repo", required=True, help="Disposable repository checkout for the agent")
    issue_run.add_argument("--memory-store", default="data/memories", help="Curated global memory store")
    issue_run.add_argument("--output-dir", required=True, help="Directory for the audited request, context, and outcome")
    issue_run.add_argument("--language")
    issue_run.add_argument("--task-id")
    issue_run.add_argument("--max-steps", type=int, default=50)
    issue_run.add_argument("--image", help="Task dependency image; required with --execute")
    issue_run.add_argument("--model", help="Gemini model; required with --execute")
    issue_run.add_argument("--terminal-timeout", type=int, default=120)
    issue_run.add_argument("--attempts", type=int, default=1)
    issue_run.add_argument("--retry-delay", type=float, default=5.0)
    issue_run.add_argument("--execute", action="store_true", help="Run OpenHands after preparing gated context")
    export_cmd = sub.add_parser("export-run", help="Export run artifacts as an untrusted review candidate")
    export_cmd.add_argument("--run-dir", required=True, help="Directory containing request.json, trajectory.json, and outcome.json")
    export_cmd.add_argument("--output", help="Candidate bundle path (defaults to review-candidate.json in run directory)")
    sb_plan = sub.add_parser("swebench-plan", help="Create a reproducible SWE-bench Lite five-condition run plan")
    sb_plan.add_argument("--output-dir", required=True)
    sb_plan.add_argument("--seed", type=int, default=7)
    sb_plan.add_argument("--model", required=True, help="Model identifier recorded in the frozen run manifest")
    sb_plan.add_argument("--max-steps", type=int, default=50)
    sb_plan.add_argument("--instance-id", action="append", help="Restrict the plan to an instance ID; repeatable")
    sb_predictions = sub.add_parser("swebench-predictions", help="Build official-harness predictions from captured run patches")
    sb_predictions.add_argument("--run-root", required=True)
    sb_predictions.add_argument("--condition", required=True, choices=CONDITIONS)
    sb_predictions.add_argument("--model", required=True)
    sb_predictions.add_argument("--output", required=True)
    sb_grade = sub.add_parser("swebench-grade", help="Grade predictions with the official SWE-bench Docker harness")
    sb_grade.add_argument("--predictions", required=True)
    sb_grade.add_argument("--run-id", required=True)
    sb_grade.add_argument("--workers", type=int, default=1)
    sb_grade.add_argument("--instance-id", action="append", help="Restrict grading to instance IDs; repeatable")
    sb_run = sub.add_parser("swebench-run", help="Prepare or execute one planned SWE-bench Lite condition")
    sb_run.add_argument("--manifest", required=True)
    sb_run.add_argument("--task-id", required=True)
    sb_run.add_argument("--condition", required=True, choices=CONDITIONS)
    sb_run.add_argument("--repo", required=True, help="Disposable checkout at the planned base_commit")
    sb_run.add_argument("--memory-store", required=True)
    sb_run.add_argument("--output-dir", required=True)
    sb_run.add_argument("--image", help="SWE-bench task image; required with --execute")
    sb_run.add_argument("--model")
    sb_run.add_argument("--terminal-timeout", type=int, default=1800)
    sb_run.add_argument("--attempts", type=int, default=1)
    sb_run.add_argument("--retry-delay", type=float, default=5.0)
    sb_run.add_argument("--execute", action="store_true")
    sub.add_parser("openhands-health")
    oh_run = sub.add_parser("openhands-run"); oh_run.add_argument("--manifest", required=True); oh_run.add_argument("--task-id", required=True); oh_run.add_argument("--condition", required=True); oh_run.add_argument("--task", required=True); oh_run.add_argument("--repo", required=True); oh_run.add_argument("--memory-store", required=True); oh_run.add_argument("--output-dir", required=True); oh_run.add_argument("--model"); oh_run.add_argument("--attempts", type=int, default=1); oh_run.add_argument("--retry-delay", type=float, default=5.0); oh_run.add_argument("--execute", action="store_true")
    oh_sandbox = sub.add_parser("openhands-sandbox-run"); oh_sandbox.add_argument("--manifest", required=True); oh_sandbox.add_argument("--task-id", required=True); oh_sandbox.add_argument("--condition", required=True); oh_sandbox.add_argument("--task", required=True); oh_sandbox.add_argument("--repo", required=True); oh_sandbox.add_argument("--memory-store", required=True); oh_sandbox.add_argument("--output-dir", required=True); oh_sandbox.add_argument("--image", required=True); oh_sandbox.add_argument("--model"); oh_sandbox.add_argument("--terminal-timeout", type=int, default=120); oh_sandbox.add_argument("--attempts", type=int, default=1); oh_sandbox.add_argument("--retry-delay", type=float, default=5.0); oh_sandbox.add_argument("--execute", action="store_true")
    context = sub.add_parser("context"); context.add_argument("--repo", required=True); context.add_argument("--file", action="append")
    execute = sub.add_parser("execute"); execute.add_argument("--repo", required=True); execute.add_argument("--image", required=True); execute.add_argument("--timeout", type=int, default=30); execute.add_argument("container_command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "export-run":
        dump(export_run(args.run_dir, args.output)); return
    if args.command == "swebench-plan":
        dump(write_swebench_plan(args.output_dir, seed=args.seed, instance_ids=args.instance_id,
                                 model=args.model, max_steps=args.max_steps)); return
    if args.command == "swebench-predictions":
        dump(export_swebench_predictions(args.run_root, args.output, condition=args.condition, model=args.model)); return
    if args.command == "swebench-grade":
        code = grade_swebench_predictions(args.predictions, run_id=args.run_id, workers=args.workers,
                                          instance_ids=args.instance_id)
        if code:
            raise SystemExit(code)
        return
    if args.command == "swebench-run":
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        matches = [item for item in manifest.get("runs", [])
                   if item.get("task_id") == args.task_id and item.get("condition") == args.condition]
        if len(matches) != 1:
            parser.error("Manifest must contain exactly one matching task ID and condition.")
        item = matches[0]
        if not item.get("task_prompt") or not item.get("base_commit"):
            parser.error("This run manifest lacks SWE-bench task prompt/base_commit metadata; regenerate it with swebench-plan.")
        head = subprocess.run(["git", "-C", args.repo, "rev-parse", "HEAD"], capture_output=True, text=True)
        if head.returncode or head.stdout.strip() != item["base_commit"]:
            parser.error(f"--repo must be checked out at base_commit {item['base_commit']}.")
        dirty = subprocess.run(["git", "-C", args.repo, "status", "--porcelain"], capture_output=True, text=True)
        if dirty.returncode or dirty.stdout.strip():
            parser.error("--repo must be a clean disposable checkout; existing changes would contaminate the prediction.")
        request = prepare_run(args.manifest, task_id=args.task_id, condition=args.condition,
                              task_prompt=item["task_prompt"], repository=args.repo,
                              memory_store=args.memory_store, output_dir=args.output_dir)
        if not args.execute:
            dump({"status": "dry_run", "request_path": str(Path(args.output_dir) / "request.json"), "request": request}); return
        selected_image = args.image or item.get("image")
        selected_model = args.model or manifest.get("model")
        if not selected_image or not selected_model:
            parser.error("swebench-run --execute requires a manifest model and either --image or a manifest image field.")
        if selected_model != manifest.get("model"):
            parser.error("The run model must match the model frozen in the evaluation manifest.")
        dump(execute_sandboxed(request, args.output_dir, image=selected_image, model=selected_model,
                               terminal_timeout_seconds=args.terminal_timeout, attempts=args.attempts,
                               retry_delay_seconds=args.retry_delay)); return
    if args.command == "ingest":
        record = normalize(args.trajectory); data = record.to_dict()
        if args.output: Path(args.output).write_text(json.dumps(data, indent=2), encoding="utf-8")
        dump(data); return
    if args.command == "pipeline":
        record = normalize(args.trajectory)
        draft = deterministic_distill(record) if args.provider == "local" else distill_with_provider(record, GeminiREST(args.model))
        if args.task_id:
            draft.provenance["task_id"] = args.task_id
        memory = draft if args.candidate_only else validate(draft, args.snapshot_root)
        path = MemoryRepository(args.store).save(memory)
        dump({"memory": memory.to_dict(), "saved_to": str(path)}); return
    if args.command == "index":
        embedder = None if args.provider == "local" else GeminiREST(args.model)
        dump(rebuild(args.store, args.output, embedder)); return
    if args.command == "health":
        records = MemoryRepository(args.store).list()
        dump({"status": "ok", "memory_count": len(records), "isolated_execution": "not_configured", "external_api_required": False}); return
    if args.command == "evaluate":
        plan = build_plan(load_tasks(args.dataset)) if args.dataset else []
        records = load_and_validate_records(args.results, plan) if args.results else []
        if args.manifest:
            write_manifest(plan, args.manifest, agent=args.agent, model=args.agent_model, seed=args.seed, budget={"max_steps": args.max_steps})
        dump(write_report(records, args.output, plan)); return
    if args.command == "agent-context":
        current_context = json.loads(Path(args.context_json).read_text(encoding="utf-8")) if args.context_json else None
        dump(memory_context(args.task, args.store, args.language, task_id=args.task_id, current_context=current_context)); return
    if args.command == "issue-run":
        request = prepare_issue_run(args.task, repository=args.repo, memory_store=args.memory_store, output_dir=args.output_dir,
                                    language=args.language, task_id=args.task_id, max_steps=args.max_steps, model=args.model)
        if not args.execute:
            dump({"status": "prepared", "request_path": str(Path(args.output_dir) / "request.json"),
                  "context_path": str(Path(args.output_dir) / "agent-context.json"), "request": request}); return
        if not args.image or not args.model:
            parser.error("issue-run --execute requires --image and --model.")
        dump(execute_sandboxed(request, args.output_dir, image=args.image, model=args.model,
                               terminal_timeout_seconds=args.terminal_timeout, attempts=args.attempts,
                               retry_delay_seconds=args.retry_delay)); return
    if args.command == "openhands-health":
        dump(openhands_health().__dict__); return
    if args.command == "openhands-run":
        request = prepare_run(args.manifest, task_id=args.task_id, condition=args.condition, task_prompt=args.task, repository=args.repo, memory_store=args.memory_store, output_dir=args.output_dir)
        if not args.execute:
            dump({"status": "dry_run", "request_path": str(Path(args.output_dir) / "request.json"), "request": request}); return
        dump(execute_prepared(request, args.output_dir, model=args.model, attempts=args.attempts, retry_delay_seconds=args.retry_delay)); return
    if args.command == "openhands-sandbox-run":
        request = prepare_run(args.manifest, task_id=args.task_id, condition=args.condition, task_prompt=args.task, repository=args.repo, memory_store=args.memory_store, output_dir=args.output_dir)
        if not args.execute:
            dump({"status": "dry_run", "request_path": str(Path(args.output_dir) / "request.json"), "image": args.image, "execution_boundary": "docker-terminal", "request": request}); return
        dump(execute_sandboxed(request, args.output_dir, image=args.image, model=args.model, terminal_timeout_seconds=args.terminal_timeout, attempts=args.attempts, retry_delay_seconds=args.retry_delay)); return
    if args.command == "context":
        dump(collect(args.repo, files=args.file)); return
    if args.command == "execute":
        container_command = args.container_command[1:] if args.container_command[:1] == ["--"] else args.container_command
        if not container_command:
            parser.error("execute requires a command after --, e.g. -- python -m pytest")
        dump(execute_if_isolated(container_command, DockerExecutor(args.repo, args.image), args.timeout).__dict__); return
    repository = MemoryRepository(args.store)
    results = retrieve(args.query, repository.list(), args.language, args.top_k)
    dump([{"memory_id": item.memory.memory_id, "score": item.score, "scores": item.scores, "status": item.memory.status,
           "gate": gate(item).__dict__, "reasons": item.reasons} for item in results])


if __name__ == "__main__":
    main()
