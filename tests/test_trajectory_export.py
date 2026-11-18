import json
import tempfile
import unittest
from pathlib import Path

from evomem.trajectory_export import export_run


class TrajectoryExportTests(unittest.TestCase):
    def make_run(self, root: Path, status: str = "completed") -> None:
        (root / "request.json").write_text(json.dumps({
            "task_id": "task-1", "condition": "baseline", "repository": "demo",
            "task_prompt": "Fix the bug", "agent_context": {"accepted_memories": []},
        }), encoding="utf-8")
        (root / "trajectory.json").write_text(json.dumps([
            {"id": "m1", "kind": "MessageEvent", "llm_message": {"content": [{"type": "text", "text": "Fix the bug"}]}},
            {"id": "a1", "kind": "ActionEvent", "tool_name": "terminal", "action": {"command": "pytest"}},
        ]), encoding="utf-8")
        (root / "outcome.json").write_text(json.dumps({"status": status, "task_id": "task-1"}), encoding="utf-8")

    def test_exports_provenance_and_does_not_promote(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_run(root)
            result = export_run(root)
            bundle = json.loads((root / "review-candidate.json").read_text(encoding="utf-8"))
            self.assertEqual(result["candidate_status"], "candidate")
            self.assertTrue(bundle["review_eligible"])
            self.assertEqual(bundle["provenance"]["task_id"], "task-1")
            self.assertEqual([event["event_id"] for event in bundle["trajectory"]["events"]], ["m1", "a1"])
            self.assertIn("no claims have been validated", bundle["review_note"])

    def test_failed_run_is_exported_but_not_review_eligible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_run(root, "failed")
            result = export_run(root)
            self.assertFalse(result["review_eligible"])

    def test_missing_artifacts_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "request"):
                export_run(temporary)


if __name__ == "__main__":
    unittest.main()
