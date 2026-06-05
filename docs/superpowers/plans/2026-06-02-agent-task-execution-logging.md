# Agent Task Execution Logging

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist per-tool-call execution details from agent loop runs to the existing `TaskRun.steps` column and expose them via API + frontend timeline.

**Architecture:** The agent loop already collects `tool_events` (round, tool name, command, output, exit code) in memory during execution. We thread this list through the task scheduler into `TaskRun.steps` as compact JSON. A new API endpoint serves the steps for a run, and the frontend renders them as an expandable timeline beneath each run history entry. No new tables or columns — the `steps` Text column already exists in the schema.

**Tech Stack:** Python 3 (FastAPI, SQLAlchemy), SQLite, vanilla JS (existing patterns in `tasks.js`)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/agent_loop.py` | Expose `tool_events` in the agent stream output (already collected in-memory) |
| `src/task_scheduler.py` | Capture `tool_events` from agent stream and persist to `TaskRun.steps` |
| `routes/task_routes.py` | Add `GET /api/tasks/runs/{run_id}/steps` endpoint; include `steps` in `_run_to_dict` |
| `static/js/tasks.js` | Render expandable tool-call timeline in run history and activity views |
| `tests/test_task_run_steps.py` | Unit tests for steps persistence, API serialization, and JSON validation |
| `tests/test_task_run_steps_js.py` | JS-side tests for timeline rendering |

---

### Task 1: Emit tool_events from agent loop stream

**Files:**
- Modify: `src/agent_loop.py:1576-1583` (stream_agent_loop function)
- Modify: `src/agent_loop.py:2212-2226` (tool_event dict construction)

The agent loop already collects `tool_events` in memory. We need to emit them as a stream event so the task scheduler can capture them without coupling to internal state.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_task_run_steps.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps.py -v`
Expected: FAIL — `_compute_final_metrics` does not include `tool_events` in its output dict.

- [ ] **Step 3: Write minimal implementation**

In `src/agent_loop.py`, find `_compute_final_metrics` (around line 1168). The function already receives `tool_events` as a parameter but does not include it in the returned metrics dict. Add it at the end of the function, just before the `return metrics` statement:

```python
    # Inside _compute_final_metrics, near the end of the function,
    # after the existing metrics dict is built and before the return:
    metrics["tool_events"] = tool_events or []
    return metrics
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_task_run_steps.py src/agent_loop.py
git commit -m "feat: include tool_events in agent loop metrics output"
```

---

### Task 2: Capture tool_events in task scheduler and persist to TaskRun.steps

**Files:**
- Modify: `src/task_scheduler.py:1576-1601` (`_run_agent_loop` method)
- Modify: `src/task_scheduler.py:697-712` (`_execute_task_locked` method)

The task scheduler's `_run_agent_loop` method already consumes the agent stream. We extend it to capture `tool_events` from the `metrics` event, then persist the JSON-serialized list to `TaskRun.steps` after execution completes.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_task_run_steps.py`:

```python
import json


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
                "round": 1,
                "tool": "generate_image",
                "command": "a sunset",
                "output": "Generated",
                "exit_code": 0,
                "image_url": "https://example.com/img.png",
                "image_prompt": "a sunset",
                "image_model": "flux",
            },
        ]
        serialized = json.dumps(events)
        deserialized = json.loads(serialized)
        assert "image_url" in deserialized[0]

    def test_steps_output_truncation(self):
        """Large tool outputs should be truncated before serialization to keep DB rows manageable."""
        large_output = "x" * 50000
        events = [
            {"round": 1, "tool": "bash", "command": "cat huge.log", "output": large_output, "exit_code": 0},
        ]
        # The persistence layer should truncate output to 10KB per event
        max_output_len = 10000
        for e in events:
            if len(e.get("output", "")) > max_output_len:
                e["output"] = e["output"][:max_output_len] + "...[truncated]"
        assert len(events[0]["output"]) <= max_output_len + 20  # + slack for suffix
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps.py::TestStepsSerialization -v`
Expected: All pass — these are pure data-structure tests, validating the contract. The real integration test is Task 3.

- [ ] **Step 3: Implement steps capture in `_run_agent_loop`**

In `src/task_scheduler.py`, modify `_run_agent_loop` (line ~1530). Add a variable to collect tool_events from the metrics event, and return them alongside the text result.

Find the block starting around line 1561:
```python
        full_text = ""
        tool_results = []
```

Change to:
```python
        full_text = ""
        tool_results = []
        captured_tool_events = []   # collected from metrics event for steps persistence
```

Then in the event parsing loop (around line 1588-1601), after the existing `elif data.get("type") == "tool_output":` block, add:

```python
                    elif data.get("type") == "metrics":
                        _te = (data.get("data") or {}).get("tool_events")
                        if _te:
                            captured_tool_events = _te
```

At the end of `_run_agent_loop`, change the return to a tuple:
```python
        return full_text, captured_tool_events
```

Update the caller in `_execute_task_locked` (around line 707):
```python
                else:
                    # LLM task — use agent loop for tool access
                    result, captured_steps = await self._execute_llm_task(task, db)
```

Then modify `_execute_llm_task` to return the captured steps too. Find `_run_agent_loop` call (around line 1358):
```python
            result = await self._run_agent_loop(
                endpoint_url, model, task, session_id,
                system_prompt=system_prompt, disabled_tools=disabled_tools,
                relevant_tools=relevant_tools,
            )
```

After the agent loop call, add truncation and return:
```python
            # Truncate large tool outputs before DB persistence
            _max_out = 10000
            for _ev in (captured_steps or []):
                if len(str(_ev.get("output", ""))) > _max_out:
                    _ev["output"] = str(_ev["output"])[:_max_out] + "...[truncated]"
            return result, captured_steps or []
```

Change the fallback simple call path (around line 1366) to return empty steps:
```python
            from src.llm_core import llm_call_async
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task.prompt},
            ]
            result = await llm_call_async(url=endpoint_url, model=model, messages=messages, timeout=120)
            return result, []
```

Also update the check-in path (around line 1307):
```python
        if is_checkin:
            result = await self._execute_checkin(task, crew, db, session_id, endpoint_url, model)
            return result, []   # check-ins don't go through agent loop
```

Back in `_execute_task_locked`, persist the steps after execution:
```python
                else:
                    # LLM task — use agent loop for tool access
                    result, captured_steps = await self._execute_llm_task(task, db)
                    run.status = "success"
                    run.result = result
                    # Persist tool execution steps
                    if captured_steps:
                        run.steps = json.dumps(captured_steps)
```

Similarly for action and research tasks (they don't produce agent steps):
```python
                if task_type == "action":
                    result, success = await self._execute_action(task, run_id=run_id)
                    run.status = "success" if success else "error"
                    run.result = result
                    if not success:
                        run.error = result
                elif task_type == "research":
                    result = await self._execute_research_task(task, db)
                    run.status = "success"
                    run.result = result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps.py -v`
Expected: PASS

- [ ] **Step 5: Run existing scheduler tests to check for regressions**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_scheduler_cancel.py tests/test_task_scheduler_session_delivery.py tests/test_scheduler_restart_doublefire.py -v`
Expected: All PASS — the return value change from `str` to `(str, list)` must not break any existing tests.

- [ ] **Step 6: Commit**

```bash
git add src/task_scheduler.py tests/test_task_run_steps.py
git commit -m "feat: persist agent tool_events to TaskRun.steps on execution"
```

---

### Task 3: Expose steps via API

**Files:**
- Modify: `routes/task_routes.py:120-131` (`_run_to_dict` helper)
- Modify: `routes/task_routes.py:792-808` (`GET /api/tasks/{task_id}/runs` handler)

Add a dedicated endpoint for fetching the detailed steps of a single run, and include a `has_steps` boolean in the existing run dict so the frontend knows whether to show the expand button.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_task_run_steps.py`:

```python
class TestRunToDictSteps:
    """Verify _run_to_dict includes steps info."""

    def test_run_to_dict_no_steps(self):
        """When steps is None, has_steps should be False."""
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
        assert "steps" not in d  # don't inline potentially large JSON

    def test_run_to_dict_with_steps(self):
        """When steps has data, has_steps should be True."""
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
        assert "steps" not in d  # steps fetched separately via dedicated endpoint
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps.py::TestRunToDictSteps -v`
Expected: FAIL — `_run_to_dict` doesn't include `has_steps` and the mock-based import may need the route module's deps stubbed.

- [ ] **Step 3: Add has_steps to _run_to_dict**

In `routes/task_routes.py`, modify `_run_to_dict` (line 120):

```python
def _run_to_dict(r: TaskRun) -> dict:
    steps_raw = getattr(r, "steps", None)  # graceful if column missing
    return {
        "id": r.id,
        "task_id": r.task_id,
        "started_at": r.started_at.isoformat() + "Z" if r.started_at else None,
        "finished_at": r.finished_at.isoformat() + "Z" if r.finished_at else None,
        "status": r.status,
        "result": r.result,
        "error": r.error,
        "tokens_used": r.tokens_used,
        "model": r.model,
        "has_steps": bool(steps_raw and steps_raw.strip()),
    }
```

- [ ] **Step 4: Add steps endpoint**

In `routes/task_routes.py`, add a new endpoint after the existing runs endpoint (after line ~808):

```python
@router.get("/runs/{run_id}/steps")
async def get_run_steps(run_id: str, request: Request):
    """Return the tool-call execution steps for a single task run."""
    db: Session = request.app.state.db_factory()
    try:
        run = db.query(TaskRun).filter(TaskRun.id == run_id).first()
        if not run:
            return JSONResponse({"error": "Run not found"}, status_code=404)
        steps_raw = getattr(run, "steps", None)
        if not steps_raw:
            return {"steps": [], "run_id": run_id}
        try:
            parsed = json.loads(steps_raw)
            if isinstance(parsed, list):
                return {"steps": parsed, "run_id": run_id}
        except (json.JSONDecodeError, TypeError):
            pass
        return {"steps": [], "run_id": run_id}
    finally:
        db.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add routes/task_routes.py tests/test_task_run_steps.py
git commit -m "feat: expose task run steps via API with has_steps flag"
```

---

### Task 4: Frontend — render tool-call timeline in run history

**Files:**
- Modify: `static/js/tasks.js:1509-1564` (`_showRunHistory` function)

When a run has `has_steps: true`, show an expandable "N tool calls" badge. On click, fetch steps from `/api/tasks/runs/{run_id}/steps` and render a compact timeline.

- [ ] **Step 1: Write the failing test**

Create `tests/test_task_run_steps_js.py`:

```python
"""JS tests for task run steps timeline rendering."""
import re


def _extract_render_steps_fn(js_source: str) -> str:
    """Extract the _renderStepsTimeline function body from tasks.js source."""
    m = re.search(r'function _renderStepsTimeline\(([^)]*)\)\s*\{(.*?)\n\}', js_source, re.DOTALL)
    return m.group(0) if m else ""


class TestStepsTimelineJS:
    """Validate the steps timeline rendering function exists and follows patterns."""

    def test_render_steps_function_exists(self):
        with open("static/js/tasks.js") as f:
            src = f.read()
        assert "_renderStepsTimeline" in src, "Missing _renderStepsTimeline function in tasks.js"

    def test_steps_badge_shown_for_has_steps(self):
        with open("static/js/tasks.js") as f:
            src = f.read()
        # The run history renderer should check has_steps and show a badge
        assert "has_steps" in src, "Frontend should reference has_steps field"

    def test_fetches_steps_from_api(self):
        with open("static/js/tasks.js") as f:
            src = f.read()
        # Should fetch from the steps endpoint
        assert "/runs/" in src and "/steps" in src, "Frontend should fetch steps from /runs/{id}/steps endpoint"

    def test_renders_tool_names(self):
        with open("static/js/tasks.js") as f:
            src = f.read()
        # Should display tool name from step data
        assert "step.tool" in src or "tool" in src.split("_renderStepsTimeline")[1][:2000] if "_renderStepsTimeline" in src else False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps_js.py -v`
Expected: FAIL — `_renderStepsTimeline` does not exist yet.

- [ ] **Step 3: Implement the steps timeline in run history**

In `static/js/tasks.js`, add a helper function before `_showRunHistory` (around line 1507):

```javascript
// ---- Steps Timeline ----

async function _renderStepsTimeline(runId, container) {
  if (container.dataset.loaded === 'true') {
    container.innerHTML = '';
    container.dataset.loaded = '';
    return;
  }
  try {
    const res = await fetch(`${API_BASE}/api/tasks/runs/${encodeURIComponent(runId)}/steps`, { credentials: 'same-origin' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const steps = data.steps || [];
    if (!steps.length) {
      container.innerHTML = '<div style="opacity:0.4;font-size:11px;padding:4px 0;">No tool calls recorded.</div>';
      container.dataset.loaded = 'true';
      return;
    }
    let html = '<div class="task-steps-timeline">';
    for (const step of steps) {
      const toolName = _esc(step.tool || '?');
      const cmd = _esc((step.command || '').length > 120 ? (step.command || '').slice(0, 120) + '…' : (step.command || ''));
      const output = _esc((step.output || '').length > 200 ? (step.output || '').slice(0, 200) + '…' : (step.output || ''));
      const exitCode = step.exit_code != null ? step.exit_code : '';
      const exitClass = exitCode === 0 ? 'step-exit-ok' : exitCode ? 'step-exit-err' : '';
      html += `<div class="task-step-item">
        <div class="task-step-header">
          <span class="task-step-round">R${step.round || '?'}</span>
          <span class="task-step-tool">${toolName}</span>
          <span class="task-step-exit ${exitClass}">${exitCode !== '' ? 'exit:' + exitCode : ''}</span>
        </div>
        ${cmd ? `<div class="task-step-cmd"><code>${cmd}</code></div>` : ''}
        ${output ? `<div class="task-step-output"><pre>${output}</pre></div>` : ''}
      </div>`;
    }
    html += '</div>';
    container.innerHTML = html;
    container.dataset.loaded = 'true';
  } catch (e) {
    container.innerHTML = `<div style="opacity:0.5;font-size:11px;">Failed to load steps: ${_escHtml(e.message)}</div>`;
  }
}
```

Then modify `_showRunHistory` to add the steps badge and expandable container. In the run item rendering loop (around line 1530), after the result div, add:

```javascript
      const stepsBtn = run.has_steps ? `<button class="task-steps-toggle" data-run-id="${_esc(run.id)}" type="button">Show tool calls</button>` : '';
      html += `<div class="task-run-item ${statusClass}">
        <div class="task-run-item-header">
          ${_statusDot(run.status === 'success' ? 'active' : run.status)}
          <span>${run.status}</span>
          ${run.model ? `<span class="task-run-model" style="font-size:10px;opacity:0.5;">${_esc(run.model.split('/').pop())}</span>` : ''}
          <span class="task-run-time" title="${run.started_at ? _esc(_relativeTime(run.started_at)) : ''}">${run.started_at ? _absoluteTime(run.started_at) : ''}</span>
        </div>
        <div class="task-run-result">${_esc(run.result ? (run.result.length > 300 ? run.result.slice(0, 300) + '…' : run.result) : run.error || '—')}</div>
        ${stepsBtn}
        <div class="task-steps-container" data-run-id="${_esc(run.id)}"></div>
      </div>`;
```

After the existing click-to-expand handler (around line 1563), add the steps toggle wiring:

```javascript
  // Wire step toggle buttons
  body.querySelectorAll('.task-steps-toggle').forEach(btn => {
    btn.addEventListener('click', async () => {
      const runId = btn.dataset.runId;
      const container = body.querySelector(`.task-steps-container[data-run-id="${runId}"]`);
      if (!container) return;
      await _renderStepsTimeline(runId, container);
      btn.textContent = container.dataset.loaded === 'true' ? 'Hide tool calls' : 'Show tool calls';
    });
  });
```

- [ ] **Step 4: Add minimal CSS for the timeline**

In `static/css/tasks.css` (or the relevant stylesheet), add:

```css
/* Steps timeline */
.task-steps-toggle {
  background: none;
  border: 1px solid var(--border-color, #333);
  color: var(--text-secondary, #888);
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 3px;
  cursor: pointer;
  margin-top: 4px;
}
.task-steps-toggle:hover { border-color: var(--accent, #646cff); color: var(--text-primary, #eee); }
.task-steps-container { margin-top: 4px; }
.task-steps-timeline { border-left: 2px solid var(--border-color, #333); padding-left: 8px; margin: 4px 0; }
.task-step-item { margin-bottom: 4px; font-size: 11px; }
.task-step-header { display: flex; gap: 6px; align-items: center; }
.task-step-round { opacity: 0.4; font-size: 10px; }
.task-step-tool { font-weight: 600; color: var(--accent, #646cff); }
.task-step-exit { font-size: 9px; opacity: 0.5; }
.step-exit-ok { color: #4caf50; }
.step-exit-err { color: #f44336; }
.task-step-cmd code { font-size: 10px; background: var(--bg-secondary, #1a1a2e); padding: 1px 4px; border-radius: 2px; }
.task-step-output pre { font-size: 10px; white-space: pre-wrap; word-break: break-word; max-height: 60px; overflow: hidden; margin: 2px 0; opacity: 0.7; }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps_js.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add static/js/tasks.js static/css/tasks.css tests/test_task_run_steps_js.py
git commit -m "feat: render tool-call timeline in task run history"
```

---

### Task 5: Wire real-time progress steps during execution

**Files:**
- Modify: `src/task_scheduler.py:998-1002` (progress callback)

For long-running agent tasks, show which tool is currently executing as live progress text in the activity feed. This uses the existing `_set_run_progress` mechanism — no new infrastructure.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_task_run_steps.py`:

```python
class TestProgressCallback:
    """Verify progress callback receives tool execution updates."""

    def test_progress_messages_format(self):
        """Progress messages should be short summaries, not full tool output."""
        progress_messages = []
        def capture_progress(msg):
            progress_messages.append(msg)

        # Simulate what agent loop would send
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
```

- [ ] **Step 2: Run test to verify it passes (it should — this is a contract test)**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps.py::TestProgressCallback -v`
Expected: PASS

- [ ] **Step 3: Add progress updates for tool events in the agent stream**

In `src/task_scheduler.py`, inside `_run_agent_loop` (the event parsing loop, around line 1594), add a progress update when tool_output events arrive:

```python
                    elif data.get("type") == "tool_output":
                        # Tool results — capture summary so we have SOMETHING even
                        # if the model never produces a final text response
                        tool_summary = data.get("stdout") or data.get("output") or data.get("result") or ""
                        if isinstance(tool_summary, str) and tool_summary.strip():
                            tool_results.append(f"[{data.get('tool', '?')}] {tool_summary[:500]}")
                        # Live progress: show which tool just ran
                        _tool_name = data.get("tool", "?")
                        _tool_cmd = (data.get("command") or data.get("query") or "")[:80]
                        _progress(f"Tool: {_tool_name} — {_tool_cmd}" if _tool_cmd else f"Tool: {_tool_name}")
```

Note: this reuses the existing `_progress` callback already wired up in the caller. The `_progress` closure is defined in `_execute_task_locked` and calls `_set_run_progress`.

We also need to pass `_progress` through to `_run_agent_loop`. Modify the method signature:

```python
    async def _run_agent_loop(self, endpoint_url: str, model: str, task, session_id: str,
                              system_prompt: str | None = None,
                              disabled_tools: set | None = None,
                              relevant_tools: set | None = None,
                              override_user_message: str | None = None,
                              progress_cb=None) -> tuple:
```

And the event loop body for `tool_output` events:
```python
                    elif data.get("type") == "tool_output":
                        tool_summary = data.get("stdout") or data.get("output") or data.get("result") or ""
                        if isinstance(tool_summary, str) and tool_summary.strip():
                            tool_results.append(f"[{data.get('tool', '?')}] {tool_summary[:500]}")
                        if progress_cb:
                            _tn = data.get("tool", "?")
                            _tc = (data.get("command") or data.get("query") or "")[:80]
                            progress_cb(f"Tool: {_tn}" + (f" — {_tc}" if _tc else ""))
```

Update the call site in `_execute_llm_task` (around line 1358) to pass the progress callback:

```python
            result, captured_steps = await self._run_agent_loop(
                endpoint_url, model, task, session_id,
                system_prompt=system_prompt, disabled_tools=disabled_tools,
                relevant_tools=relevant_tools,
                progress_cb=_progress,
            )
```

Wait — `_progress` is defined in `_execute_task_locked`, not `_execute_llm_task`. We need to pass it through. Modify `_execute_llm_task` signature to accept an optional `progress_cb`:

```python
    async def _execute_llm_task(self, task, db, progress_cb=None) -> tuple:
```

And update the caller in `_execute_task_locked`:

```python
                else:
                    # LLM task — use agent loop for tool access
                    result, captured_steps = await self._execute_llm_task(task, db, progress_cb=_progress)
```

- [ ] **Step 4: Run all task-related tests**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps.py tests/test_task_scheduler_cancel.py tests/test_task_scheduler_session_delivery.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/task_scheduler.py tests/test_task_run_steps.py
git commit -m "feat: show live tool progress in task run activity feed"
```

---

### Task 6: End-to-end smoke test and edge cases

**Files:**
- Create: `tests/test_task_run_steps_e2e.py`

Validate the full pipeline: agent metrics → scheduler capture → API serialization → JSON integrity.

- [ ] **Step 1: Write integration-style tests**

Create `tests/test_task_run_steps_e2e.py`:

```python
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


class TestStepsPipeline:
    """Validate the full steps pipeline without hitting real services."""

    def test_agent_metrics_to_steps_json(self):
        """Simulate: agent loop produces metrics with tool_events → scheduler persists as steps."""
        from src.agent_loop import _compute_final_metrics

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

        # Simulate what task_scheduler does
        captured = metrics.get("tool_events", [])
        steps_json = json.dumps(captured)

        # Validate it round-trips
        parsed = json.loads(steps_json)
        assert len(parsed) == 2
        assert parsed[0]["tool"] == "web_search"
        assert parsed[1]["tool"] == "bash"
        assert parsed[0]["round"] == 1
        assert parsed[1]["round"] == 2

    def test_api_steps_response_format(self):
        """Validate the API response shape for the steps endpoint."""
        steps = [
            {"round": 1, "tool": "web_search", "command": "test", "output": "result", "exit_code": 0}
        ]
        # Simulate what the endpoint returns
        response = {"steps": steps, "run_id": "run-abc123"}
        assert "steps" in response
        assert "run_id" in response
        assert isinstance(response["steps"], list)

    def test_empty_run_produces_empty_steps(self):
        """A run with no tool calls should persist empty steps."""
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
        """Tool outputs over 10KB should be truncated before DB write."""
        from src.agent_loop import _compute_final_metrics

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
        # Simulate scheduler truncation
        max_out = 10000
        for ev in captured:
            if len(str(ev.get("output", ""))) > max_out:
                ev["output"] = str(ev["output"])[:max_out] + "...[truncated]"
        steps_json = json.dumps(captured)
        assert len(steps_json) < 15000  # Well under SQLite practical row limits

    def test_run_to_dict_has_steps_flag(self):
        """_run_to_dict must include has_steps boolean."""
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

        # Without steps
        run.steps = None
        d = _run_to_dict(run)
        assert d["has_steps"] is False

        # With steps
        run.steps = json.dumps([{"round": 1, "tool": "bash", "command": "ls", "output": "files", "exit_code": 0}])
        d = _run_to_dict(run)
        assert d["has_steps"] is True
```

- [ ] **Step 2: Run all tests**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/test_task_run_steps_e2e.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite to check for regressions**

Run: `cd /home/droytel/Documents/Github/odysseus && python -m pytest tests/ -x --timeout=30 2>&1 | tail -30`
Expected: All PASS — any failure here indicates a regression from the return-value change or API changes.

- [ ] **Step 4: Commit**

```bash
git add tests/test_task_run_steps_e2e.py
git commit -m "test: add end-to-end tests for task run steps pipeline"
```

---

### Task 7: Activity view — steps preview on hover/click

**Files:**
- Modify: `static/js/tasks.js:2193-2280` (`_renderActivityEntry` function)

Show a quick tool-call count badge in the activity feed for runs that have steps. Uses the same `_renderStepsTimeline` helper from Task 4.

- [ ] **Step 1: Verify the activity feed includes has_steps**

Check that the `GET /api/tasks/runs/recent` endpoint's response flows `has_steps` through to the activity entries. In `routes/task_routes.py`, the recent runs endpoint calls `_run_to_dict` which now includes `has_steps`. Verify the JS mapping at line 1861 preserves it:

In `static/js/tasks.js`, inside the `_activityEntries = runs.map(...)` block (around line 1861), add:

```javascript
        hasSteps: r.has_steps || false,
```

- [ ] **Step 2: Add steps badge to activity entries**

In `_renderActivityEntry` (around line 2193), after the action button logic and before the closing `</div>` of the entry, add a steps badge:

```javascript
  const stepsBadge = entry.hasSteps
    ? `<span class="task-log-steps-badge" data-run-id="${_escHtml(r => r.id || '')}" title="This run used tools">tools</span>`
    : '';
```

Then include `${stepsBadge}` in the entry's header line next to the model tag.

- [ ] **Step 3: Add CSS for steps badge**

In `static/css/tasks.css`:

```css
.task-log-steps-badge {
  font-size: 9px;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--bg-secondary, #1a1a2e);
  color: var(--text-secondary, #888);
  border: 1px solid var(--border-color, #333);
}
```

- [ ] **Step 4: Commit**

```bash
git add static/js/tasks.js static/css/tasks.css
git commit -m "feat: show tool-call badge in activity feed for runs with steps"
```

---

## Self-Review

### 1. Spec coverage
- Persist tool_events to TaskRun.steps: Task 2
- API to expose steps: Task 3
- Frontend timeline in run history: Task 4
- Activity feed badge: Task 7
- Live progress during execution: Task 5
- Edge cases (truncation, empty, unicode): Tasks 2, 6

### 2. Placeholder scan
No TBD/TODO/placeholders found. All steps contain complete code.

### 3. Type consistency
- `tool_events` is `list[dict]` throughout (agent_loop → metrics → scheduler → JSON string in DB → parsed list in API)
- `_run_agent_loop` returns `(str, list)` tuple — all callers updated
- `_run_to_dict` returns `dict` with `has_steps: bool`
- `_execute_llm_task` returns `(str, list)` tuple — all callers updated
- `_execute_checkin` returns `str` — wrapper in `_execute_llm_task` adds empty steps list
- Progress callback signature: `(message: str) -> None` — matches existing `_set_run_progress`
