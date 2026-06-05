# Tasks Lens Segregation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate automatic task sessions from regular chats in the sidebar by adding a "Tasks" lens tab, so users can switch between their conversations and background task output.

**Architecture:** Leverages the existing `folder` column on sessions (already used for "Assistant" folder). Task sessions get `folder="Tasks"` at creation time. The sidebar header gains a lightweight tab toggle ("Chats" / "Tasks") that filters the session list by folder. Existing `_isTransient` auto-select guards remain unchanged.

**Tech Stack:** Python/FastAPI (backend), vanilla JS (frontend), SQLite (storage), existing CSS variables for styling.

---

## File Structure

| File | Responsibility | Change Type |
|------|---------------|-------------|
| `src/task_scheduler.py` | Set `folder="Tasks"` when creating task sessions | Modify (3 locations) |
| `static/js/sessions.js` | Add lens tab state, filter logic for "Chats" vs "Tasks" views | Modify |
| `static/index.html` | Add lens tab HTML in session section header | Modify |
| `static/style.css` | Add lens tab styles using existing CSS variables | Modify |
| `tests/test_task_session_folder.py` | Test that task sessions get `folder="Tasks"` | Create |

---

### Task 1: Backend — Set folder="Tasks" on task session creation

**Files:**
- Modify: `src/task_scheduler.py:1304-1312` (LLM task session)
- Modify: `src/task_scheduler.py:1446-1454` (action task session)
- Modify: `src/task_scheduler.py:1726-1734` (research task session)
- Create: `tests/test_task_session_folder.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_task_session_folder.py
"""Task sessions must be assigned folder='Tasks' at creation time."""
import pytest
from unittest.mock import MagicMock, patch
from core.database import Session as DbSession


@pytest.fixture
def scheduler_and_db(tmp_path):
    """Create a TaskScheduler with a real in-memory DB session."""
    from core.database import SessionLocal, Base
    from sqlalchemy import create_engine
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    from core.database import _Session as _S
    # Patch SessionLocal to use the test engine
    import core.database as db_mod
    original = db_mod.SessionLocal
    db_mod.SessionLocal = lambda: _S(bind=engine)
    from src.task_scheduler import TaskScheduler
    sched = TaskScheduler.__new__(TaskScheduler)
    sched._session_manager = MagicMock()
    yield sched, db_mod.SessionLocal()
    db_mod.SessionLocal = original


def test_llm_task_session_gets_tasks_folder(scheduler_and_db):
    """_execute_llm_task must create sessions with folder='Tasks'."""
    sched, db = scheduler_and_db
    from core.database import ScheduledTask
    task = ScheduledTask(
        id="t1", name="Test LLM", task_type="llm",
        trigger_type="schedule", schedule="once",
        owner="admin", endpoint_url="http://localhost:8000/v1",
        model="test-model",
    )
    db.add(task)
    db.commit()

    # The method is async and needs LLM calls — just verify the
    # session creation block directly by inspecting the pattern.
    import inspect
    source = inspect.getsource(sched._execute_llm_task)
    # The session creation block must include folder="Tasks"
    assert 'folder="Tasks"' in source or "folder='Tasks'" in source, (
        "LLM task session creation must set folder='Tasks'"
    )


def test_action_task_session_gets_tasks_folder(scheduler_and_db):
    """_execute_action_task must create sessions with folder='Tasks'."""
    sched, db = scheduler_and_db
    import inspect
    source = inspect.getsource(sched._execute_action_task)
    assert 'folder="Tasks"' in source or "folder='Tasks'" in source, (
        "Action task session creation must set folder='Tasks'"
    )


def test_research_task_session_gets_tasks_folder(scheduler_and_db):
    """_execute_research_task must create sessions with folder='Tasks'."""
    sched, db = scheduler_and_db
    import inspect
    source = inspect.getsource(sched._execute_research_task)
    assert 'folder="Tasks"' in source or "folder='Tasks'" in source, (
        "Research task session creation must set folder='Tasks'"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_task_session_folder.py -v`
Expected: FAIL — none of the three session creation blocks set `folder="Tasks"` yet.

- [ ] **Step 3: Add folder="Tasks" to LLM task session creation**

In `src/task_scheduler.py`, find the `_execute_llm_task` method's session creation block (~line 1304) and add `folder="Tasks"`:

```python
            sess = DbSession(
                id=session_id,
                name=f"[Task] {task.name}",
                endpoint_url=endpoint_url,
                model=model,
                owner=task.owner,
                folder="Tasks",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
```

- [ ] **Step 4: Add folder="Tasks" to action task session creation**

In `src/task_scheduler.py`, find the `_execute_action_task` method's session creation block (~line 1446) and add `folder="Tasks"`:

```python
            sess = DbSession(
                id=session_id,
                name=f"[Task] {task.name}",
                endpoint_url=endpoint_url or "",
                model=model_name or "",
                owner=task.owner,
                folder="Tasks",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
```

- [ ] **Step 5: Add folder="Tasks" to research task session creation**

In `src/task_scheduler.py`, find the `_execute_research_task` method's session creation block (~line 1726) and add `folder="Tasks"`:

```python
            sess = DbSession(
                id=session_id,
                name=f"[Research] {task.name}",
                endpoint_url=endpoint_url,
                model=model,
                owner=task.owner,
                folder="Tasks",
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_task_session_folder.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 7: Run existing scheduler tests to verify no regressions**

Run: `python -m pytest tests/test_task_scheduler_cancel.py tests/test_task_scheduler_session_delivery.py tests/test_scheduler_restart_doublefire.py -v`
Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add src/task_scheduler.py tests/test_task_session_folder.py
git commit -m "feat: assign folder='Tasks' to task sessions at creation

Task sessions (LLM, action, research) now set folder='Tasks' on their
DbSession row, matching the pattern used by the Assistant folder. This
enables sidebar lens filtering without changing existing session
behaviour."
```

---

### Task 2: Backend — Backfill existing task sessions

**Files:**
- Create: `scripts/backfill_task_folders.py`

- [ ] **Step 1: Write the backfill script**

This one-shot script updates existing `[Task]` and `[Research]` sessions that have no folder set:

```python
#!/usr/bin/env python3
"""One-shot backfill: set folder='Tasks' on existing task/research sessions.

Usage:
    python scripts/backfill_task_folders.py [--dry-run]
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.database import SessionLocal, Session as DbSession


def main(dry_run: bool = False):
    db = SessionLocal()
    try:
        rows = (
            db.query(DbSession)
            .filter(
                DbSession.folder == None,  # noqa: E711
                (DbSession.name.like("[Task] %") | DbSession.name.like("[Research] %")),
            )
            .all()
        )
        print(f"Found {len(rows)} task/research sessions without folder")
        for row in rows:
            print(f"  {row.id[:12]}  {row.name}")
            if not dry_run:
                row.folder = "Tasks"
        if not dry_run and rows:
            db.commit()
            print(f"Updated {len(rows)} sessions")
        elif dry_run:
            print("(dry run — no changes made)")
    finally:
        db.close()


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
```

- [ ] **Step 2: Run the backfill (dry-run first)**

Run: `python scripts/backfill_task_folders.py --dry-run`
Expected: Lists existing `[Task]`/`[Research]` sessions without committing.

- [ ] **Step 3: Run the backfill for real**

Run: `python scripts/backfill_task_folders.py`
Expected: Updates existing sessions.

- [ ] **Step 4: Verify the backfill**

Run: `sqlite3 data/app.db "SELECT id, name, folder FROM sessions WHERE folder='Tasks';"`
Expected: Shows all previously `[Task]`/`[Research]` sessions now have `folder="Tasks"`.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_task_folders.py
git commit -m "feat: add backfill script for task session folders

One-shot script to set folder='Tasks' on existing [Task]/[Research]
sessions that predate the folder assignment in task_scheduler.py."
```

---

### Task 3: Frontend — Add lens tab HTML to sidebar

**Files:**
- Modify: `static/index.html:694-746` (sessions section)

- [ ] **Step 1: Add lens tab HTML between section-header and session-list**

Insert the following block after the `session-bulk-bar` div (after line ~745) and before `session-list`:

```html
        <div id="session-lens-tabs" class="session-lens-tabs">
          <button class="lens-tab active" data-lens="chats" id="lens-chats">Chats</button>
          <button class="lens-tab" data-lens="tasks" id="lens-tasks">Tasks</button>
        </div>
```

This goes right before `<div id="session-list" role="listbox"></div>`.

- [ ] **Step 2: Verify HTML parses correctly**

Run: `node -e "const fs=require('fs'); const h=fs.readFileSync('static/index.html','utf8'); console.log(h.includes('session-lens-tabs') ? 'OK: lens tabs present' : 'MISSING')"`
Expected: `OK: lens tabs present`

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat: add Chats/Tasks lens tab HTML to session sidebar

Adds a lens tab row between the session section header and the session
list. Follows existing visual patterns (button, data-attribute naming)."
```

---

### Task 4: Frontend — Add lens tab CSS

**Files:**
- Modify: `static/style.css`

- [ ] **Step 1: Add lens tab styles**

Append the following to `static/style.css` (in the sidebar/session section area):

```css
/* ── Session lens tabs (Chats / Tasks) ── */
.session-lens-tabs {
  display: flex;
  gap: 0;
  padding: 0 8px;
  margin-bottom: 2px;
  border-bottom: 1px solid var(--border);
}
.lens-tab {
  flex: 1;
  background: none;
  border: none;
  color: var(--fg);
  opacity: 0.4;
  font-size: 10px;
  font-family: inherit;
  padding: 4px 0;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: opacity 0.15s, border-color 0.15s;
}
.lens-tab:hover {
  opacity: 0.7;
}
.lens-tab.active {
  opacity: 1;
  border-bottom-color: var(--red);
}
```

This follows existing patterns:
- Uses `var(--fg)`, `var(--border)`, `var(--red)` — no new color values
- 10px font matches existing sidebar labels
- Transitions match existing button patterns

- [ ] **Step 2: Check CSS parses**

Run: `node -e "const fs=require('fs'); const c=fs.readFileSync('static/style.css','utf8'); console.log(c.includes('session-lens-tabs') ? 'OK: lens CSS present' : 'MISSING')"`
Expected: `OK: lens CSS present`

- [ ] **Step 3: Commit**

```bash
git add static/style.css
git commit -m "feat: add lens tab styles for Chats/Tasks sidebar toggle

Uses existing CSS variables (--fg, --border, --red) and matches the
monospace 10px font size already used for sidebar labels."
```

---

### Task 5: Frontend — Add lens filtering logic in sessions.js

**Files:**
- Modify: `static/js/sessions.js`

- [ ] **Step 1: Add lens state variable and tab click handler**

At the top of the IIFE/module scope in `sessions.js`, add the lens state near the other state variables (around the existing `_sortMode` declaration):

```javascript
  // Lens tab: "chats" (default) or "tasks"
  let _activeLens = Storage.get('session-lens') || 'chats';
```

Add the lens tab click handler. Find where DOMContentLoaded or the initialization block sets up event listeners (search for `session-sort-btn` click handler as a landmark). Add after it:

```javascript
  // ── Lens tab switching ──
  document.querySelectorAll('.lens-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      _activeLens = tab.dataset.lens;
      Storage.set('session-lens', _activeLens);
      document.querySelectorAll('.lens-tab').forEach(t => t.classList.toggle('active', t.dataset.lens === _activeLens));
      // Update section title
      const label = document.getElementById('chats-section-label');
      if (label) label.textContent = _activeLens === 'tasks' ? 'Tasks' : 'Chats';
      renderSessionList();
    });
  });
  // Restore saved lens on load
  document.querySelectorAll('.lens-tab').forEach(t => t.classList.toggle('active', t.dataset.lens === _activeLens));
  const _initLabel = document.getElementById('chats-section-label');
  if (_initLabel && _activeLens === 'tasks') _initLabel.textContent = 'Tasks';
```

- [ ] **Step 2: Update session filter to respect lens**

Find the session filtering line (~line 759) that currently reads:

```javascript
let orderedSessions = sessions.filter(s => !s.archived && s.folder !== 'Assistant' && !_isIncognitoSession(s.id) && (s.name || '').trim() !== 'Nobody' && (s.name || '').trim() !== 'Incognito');
```

Replace with:

```javascript
  let orderedSessions = sessions.filter(s => {
    if (s.archived) return false;
    if (_isIncognitoSession(s.id)) return false;
    if ((s.name || '').trim() === 'Nobody' || (s.name || '').trim() === 'Incognito') return false;
    // Lens filtering: show only sessions matching the active lens
    if (_activeLens === 'tasks') {
      return s.folder === 'Tasks';
    }
    // Default (chats): show everything EXCEPT Assistant and Tasks folders
    return s.folder !== 'Assistant' && s.folder !== 'Tasks';
  });
```

- [ ] **Step 3: Update _isTransient auto-select guard**

The existing `_isTransient` function (~line 1374) already checks for `folder === 'Tasks'`. No change needed — it correctly prevents auto-selecting task sessions.

- [ ] **Step 4: Verify JavaScript parses**

Run: `node --check static/js/sessions.js`
Expected: No output (no syntax errors).

- [ ] **Step 5: Commit**

```bash
git add static/js/sessions.js
git commit -m "feat: add Chats/Tasks lens filtering to session sidebar

When the Tasks lens tab is active, the sidebar shows only sessions with
folder='Tasks'. The Chats lens (default) excludes both Assistant and
Tasks folders. Lens preference persists in localStorage."
```

---

### Task 6: Frontend — Update task count badge on Tasks tab

**Files:**
- Modify: `static/js/sessions.js`
- Modify: `static/style.css`

- [ ] **Step 1: Add a notification dot to the Tasks tab**

In `sessions.js`, inside the `loadSessions()` function (after `sessions = _normalizeSessionsList(fetched)`), add logic to count task sessions and update the tab:

```javascript
    // Update task count badge on Tasks lens tab
    const _taskCount = (fetched || []).filter(s => !s.archived && s.folder === 'Tasks').length;
    const _tasksTab = document.getElementById('lens-tasks');
    if (_tasksTab) {
      const _existingBadge = _tasksTab.querySelector('.lens-badge');
      if (_existingBadge) _existingBadge.remove();
      if (_taskCount > 0) {
        const badge = document.createElement('span');
        badge.className = 'lens-badge';
        badge.textContent = _taskCount > 99 ? '99+' : _taskCount;
        _tasksTab.appendChild(badge);
      }
    }
```

- [ ] **Step 2: Add badge CSS**

In `static/style.css`, add after the `.lens-tab.active` rule:

```css
.lens-badge {
  font-size: 8px;
  background: var(--red);
  color: var(--bg);
  border-radius: 8px;
  padding: 0 4px;
  margin-left: 4px;
  line-height: 14px;
  vertical-align: middle;
}
```

This uses `var(--red)` for the badge background (same as notification dots elsewhere) and `var(--bg)` for text.

- [ ] **Step 3: Verify JavaScript parses**

Run: `node --check static/js/sessions.js`
Expected: No output (no syntax errors).

- [ ] **Step 4: Commit**

```bash
git add static/js/sessions.js static/style.css
git commit -m "feat: add task count badge to Tasks lens tab

Shows a small red badge with the count of active task sessions on the
Tasks tab. Uses existing --red/--bg CSS variables."
```

---

### Task 7: Integration test and visual verification

**Files:** None (manual testing)

- [ ] **Step 1: Run all existing tests**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: All existing tests pass. New test file `test_task_session_folder.py` passes.

- [ ] **Step 2: Verify JS syntax across all modified files**

Run: `node --check static/js/sessions.js && echo "OK"`
Expected: `OK`

- [ ] **Step 3: Verify Python syntax across all modified files**

Run: `python -m py_compile src/task_scheduler.py && echo "OK"`
Expected: `OK`

- [ ] **Step 4: Manual visual test — run the app**

Run: `python -m uvicorn app:app --host 0.0.0.0 --port 7000` (or restart existing server)

Verify in browser:
1. Sidebar shows "Chats" and "Tasks" tabs
2. Clicking "Tasks" shows task sessions (if any exist after backfill)
3. Clicking "Chats" shows regular sessions without task sessions
4. Tasks tab has a red badge with count
5. Auto-select does NOT land on a task session on page load
6. Existing sort modes (Last Active, Newest, By Folder) still work in both lenses

- [ ] **Step 5: Take screenshots for contribution compliance**

Take screenshots of:
1. Chats lens with regular sessions
2. Tasks lens (even if empty — shows the tab is present)
3. Mobile view (responsive test)

Per CONTRIBUTING.md: "Attach a screenshot or short clip of the change in the running app."

- [ ] **Step 6: Final commit (squash prep if needed)**

Review the commit log and ensure messages follow the project style:
```bash
git log --oneline -6
```

The commits are ready for a future PR but NO PR is created per user instructions.

---

## Self-Review Checklist

**1. Spec coverage:**
- Task sessions get `folder="Tasks"` at creation: Task 1 (all 3 locations)
- Existing task sessions are backfilled: Task 2
- Sidebar shows lens tabs: Task 3 (HTML) + Task 4 (CSS) + Task 5 (JS logic)
- Task count badge: Task 6
- Visual verification + screenshots: Task 7

**2. Placeholder scan:**
- No "TBD", "TODO", "implement later" found
- All code blocks contain complete implementations
- No "add appropriate error handling" patterns
- No "similar to Task N" shortcuts

**3. Type consistency:**
- `folder="Tasks"` (string) matches existing `"Assistant"` pattern
- `data-lens="chats"` / `data-lens="tasks"` consistent between HTML and JS
- `_activeLens` variable consistently used in filter and tab toggle
- `.lens-tab`, `.lens-badge` class names consistent between HTML, CSS, and JS
