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
