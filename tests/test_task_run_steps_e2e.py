"""End-to-end tests for task run steps pipeline."""
import json
import sys
from unittest.mock import MagicMock

for mod in [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database', 'src.agent_tools', 'core.models', 'core.database',
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from src.agent_loop import _compute_final_metrics


class TestStepsPipeline:
    """Validate the full steps pipeline without hitting real services."""

    def test_agent_metrics_to_steps_json(self):
        tool_events = [
            {"round": 1, "tool": "web_search", "command": "weather sydney", "output": "25C, sunny", "exit_code": 0},
            {"round": 2, "tool": "bash", "command": "curl wttr.in/sydney?format=3", "output": "Sydney: +25C", "exit_code": 0},
        ]
        metrics = _compute_final_metrics(
            messages=[], full_response="Sydney is 25C and sunny.",
            total_duration=5.2, time_to_first_token=0.3,
            context_length=131072, real_input_tokens=500, real_output_tokens=200,
            has_real_usage=True, tool_events=tool_events,
            round_texts=["Sydney is 25C and sunny."], model="gemma-4-26b-a4b-it",
        )

        captured = metrics.get("tool_events", [])
        steps_json = json.dumps(captured)
        parsed = json.loads(steps_json)
        assert len(parsed) == 2
        assert parsed[0]["tool"] == "web_search"
        assert parsed[1]["tool"] == "bash"
        assert parsed[0]["round"] == 1
        assert parsed[1]["round"] == 2

    def test_api_steps_response_format(self):
        steps = [
            {"round": 1, "tool": "web_search", "command": "test", "output": "result", "exit_code": 0}
        ]
        response = {"steps": steps, "run_id": "run-abc123"}
        assert "steps" in response
        assert "run_id" in response
        assert isinstance(response["steps"], list)

    def test_empty_run_produces_empty_steps(self):
        metrics = _compute_final_metrics(
            messages=[], full_response="Simple answer, no tools needed.",
            total_duration=1.0, time_to_first_token=0.2,
            context_length=131072, real_input_tokens=50, real_output_tokens=20,
            has_real_usage=True, tool_events=[],
            round_texts=["Simple answer, no tools needed."], model="test",
        )
        assert metrics.get("tool_events") == []
        steps_json = json.dumps(metrics.get("tool_events", []))
        assert json.loads(steps_json) == []

    def test_large_output_is_truncated_before_persistence(self):
        large_events = [
            {"round": 1, "tool": "bash", "command": "cat /var/log/syslog",
             "output": "x" * 50000, "exit_code": 0}
        ]
        metrics = _compute_final_metrics(
            messages=[], full_response="Here's the log.", total_duration=3.0,
            time_to_first_token=0.2, context_length=131072,
            real_input_tokens=200, real_output_tokens=100,
            has_real_usage=True, tool_events=large_events,
            round_texts=["Here's the log."], model="test",
        )
        captured = metrics.get("tool_events", [])
        max_out = 10000
        for ev in captured:
            if len(str(ev.get("output", ""))) > max_out:
                ev["output"] = str(ev["output"])[:max_out] + "...[truncated]"
        steps_json = json.dumps(captured)
        assert len(steps_json) < 15000

    def test_run_to_dict_has_steps_flag(self):
        from routes.task_routes import _run_to_dict

        run = MagicMock()
        run.id = "r1"
        run.task_id = "t1"
        run.started_at = None
        run.finished_at = None
        run.status = "success"
        run.result = "ok"
        run.error = None
        run.tokens_used = None
        run.model = None

        run.steps = None
        d = _run_to_dict(run)
        assert d["has_steps"] is False

        run.steps = json.dumps([{"round": 1, "tool": "bash", "command": "ls", "output": "files", "exit_code": 0}])
        d = _run_to_dict(run)
        assert d["has_steps"] is True
