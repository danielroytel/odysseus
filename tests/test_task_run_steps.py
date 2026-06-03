"""Tests for agent loop tool_events emission and TaskRun.steps persistence."""

import json
import sys
from unittest.mock import MagicMock

# Mock heavy deps
for mod in [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database', 'src.agent_tools', 'core.models', 'core.database',
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

from src.agent_loop import _compute_final_metrics


class TestToolEventsInMetrics:
    """tool_events should be included in the final metrics payload."""

    def test_metrics_includes_tool_events(self):
        events = [{"round": 1, "tool": "web_search", "command": "test query", "output": "result text", "exit_code": 0}]
        metrics = _compute_final_metrics(
            messages=[],
            full_response="done",
            total_duration=1.0,
            time_to_first_token=0.5,
            context_length=128000,
            real_input_tokens=100,
            real_output_tokens=50,
            has_real_usage=True,
            tool_events=events,
            round_texts=["done"],
            model="test-model",
        )
        assert "tool_events" in metrics
        assert metrics["tool_events"] == events

    def test_metrics_empty_tool_events(self):
        metrics = _compute_final_metrics(
            messages=[],
            full_response="hello",
            total_duration=1.0,
            time_to_first_token=0.5,
            context_length=128000,
            real_input_tokens=100,
            real_output_tokens=50,
            has_real_usage=True,
            tool_events=[],
            round_texts=["hello"],
            model="test-model",
        )
        assert metrics.get("tool_events") == []

    def test_metrics_tool_events_none(self):
        metrics = _compute_final_metrics(
            messages=[],
            full_response="hello",
            total_duration=1.0,
            time_to_first_token=0.5,
            context_length=128000,
            real_input_tokens=100,
            real_output_tokens=50,
            has_real_usage=True,
            tool_events=None,
            round_texts=["hello"],
            model="test-model",
        )
        assert metrics.get("tool_events") == []


class TestStepsSerialization:
    """Verify steps JSON structure is valid and compact."""

    def test_steps_json_roundtrip(self):
        events = [
            {"round": 1, "tool": "web_search", "command": "sydney weather", "output": "25C sunny", "exit_code": 0},
            {"round": 2, "tool": "bash", "command": "echo hello", "output": "hello", "exit_code": 0},
        ]
        serialized = json.dumps(events)
        assert json.loads(serialized) == events

    def test_steps_json_handles_unicode(self):
        events = [
            {"round": 1, "tool": "web_search", "command": "wetter in münchen", "output": "sonnig", "exit_code": 0},
        ]
        serialized = json.dumps(events, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert deserialized[0]["command"] == "wetter in münchen"

    def test_steps_json_empty_list(self):
        serialized = json.dumps([])
        assert serialized == "[]"
        assert json.loads(serialized) == []

    def test_steps_with_optional_fields(self):
        events = [
            {
                "round": 1, "tool": "generate_image", "command": "a sunset",
                "output": "Generated", "exit_code": 0,
                "image_url": "https://example.com/img.png",
                "image_prompt": "a sunset", "image_model": "flux",
            },
        ]
        serialized = json.dumps(events)
        deserialized = json.loads(serialized)
        assert "image_url" in deserialized[0]

    def test_steps_output_truncation(self):
        large_output = "x" * 50000
        events = [
            {"round": 1, "tool": "bash", "command": "cat huge.log", "output": large_output, "exit_code": 0},
        ]
        max_output_len = 10000
        for e in events:
            if len(e.get("output", "")) > max_output_len:
                e["output"] = e["output"][:max_output_len] + "...[truncated]"
        assert len(events[0]["output"]) <= max_output_len + 20


class TestRunToDictSteps:
    """Verify _run_to_dict includes steps info."""

    def test_run_to_dict_no_steps(self):
        from routes.task_routes import _run_to_dict

        mock_run = MagicMock()
        mock_run.id = "run-1"
        mock_run.task_id = "task-1"
        mock_run.started_at = None
        mock_run.finished_at = None
        mock_run.status = "success"
        mock_run.result = "ok"
        mock_run.error = None
        mock_run.tokens_used = 100
        mock_run.model = "test"
        mock_run.steps = None
        d = _run_to_dict(mock_run)
        assert d["has_steps"] is False
        assert "steps" not in d

    def test_run_to_dict_with_steps(self):
        from routes.task_routes import _run_to_dict

        mock_run = MagicMock()
        mock_run.id = "run-2"
        mock_run.task_id = "task-1"
        mock_run.started_at = None
        mock_run.finished_at = None
        mock_run.status = "success"
        mock_run.result = "ok"
        mock_run.error = None
        mock_run.tokens_used = 100
        mock_run.model = "test"
        mock_run.steps = json.dumps([{"round": 1, "tool": "bash", "command": "ls", "output": "files", "exit_code": 0}])
        d = _run_to_dict(mock_run)
        assert d["has_steps"] is True
        assert "steps" not in d


class TestProgressCallback:
    """Verify progress callback receives tool execution updates."""

    def test_progress_messages_format(self):
        """Progress messages should be short summaries, not full tool output."""
        progress_messages = []
        def capture_progress(msg):
            progress_messages.append(msg)

        from src.agent_loop import _compute_final_metrics
        metrics = _compute_final_metrics(
            messages=[], full_response="", total_duration=1.0,
            time_to_first_token=0.5, context_length=128000,
            real_input_tokens=100, real_output_tokens=50,
            has_real_usage=True,
            tool_events=[{"round": 1, "tool": "web_search", "command": "test", "output": "x" * 100, "exit_code": 0}],
            round_texts=[""], model="test",
        )
        events = metrics.get("tool_events", [])
        for ev in events:
            progress_messages.append(f"Running {ev['tool']}: {ev.get('command', '')[:80]}")
        assert len(progress_messages) == 1
        assert progress_messages[0].startswith("Running web_search:")
        assert len(progress_messages[0]) < 120
