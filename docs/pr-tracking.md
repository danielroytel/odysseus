# PR & Issue Tracking — Odysseus Fork

Following CONTRIBUTING.md: open issues first for large features, one bug/feature per PR, run the app to verify.

## Our Open PRs (pending template compliance)

### PR #1842: Agent task execution logging
- **Branch:** `fix/agent-task-execution-logging`
- **Type:** New feature
- **Status:** Open — needs reformat
- **Template gaps:** No Linked Issue, no Type of Change checklist, no How to Test steps, no screenshots
- **Action:** Close, create issue, re-submit

### PR #1843: Per-endpoint context window override
- **Branch:** `feat/endpoint-context-override`
- **Type:** New feature
- **Status:** Open — needs reformat
- **Template gaps:** No Linked Issue, no Type of Change checklist, no How to Test steps, no UI screenshot
- **Action:** Close, create issue, re-submit

### PR #1844: Model context window entries
- **Branch:** `fix/model-context-windows`
- **Type:** Bug fix / data correction
- **Status:** Open — needs reformat
- **Template gaps:** No Linked Issue, no Type of Change checklist, no How to Test steps
- **Action:** Close, create issue, re-submit

### PR #1642: Gemma 4 thinking model patterns
- **Status:** MERGED — no action needed

---

## Draft Issues (to open on upstream before re-submitting PRs)

### Issue A: Agent task execution tool-call logging

**Title:** Agent task runs have no visibility into which tools were executed

**Body:**
When a scheduled task runs via the agent loop, the `TaskRun.steps` column exists but is never populated. After execution completes, the activity feed shows only the final text output — there's no way to see which tools the agent invoked, what commands ran, or whether they succeeded.

This makes debugging failed tasks difficult — you can see the agent's final answer but not the reasoning steps or tool failures that led to it.

**Proposed approach:** Capture `tool_events` from the agent loop SSE stream and persist them as JSON in `TaskRun.steps`. Expose via a `/runs/{id}/steps` API endpoint and render as an expandable timeline in the task run history UI.

**Install method:** Manual Python on Linux
**Repro steps:**
1. Create a scheduled LLM task that uses agent tools (e.g., "search for X")
2. Let it complete
3. Open the run history — no tool execution details are shown
4. Check database: `TaskRun.steps` column is NULL

---

### Issue B: Per-endpoint context window override

**Title:** No way to override context window size per model endpoint

**Body:**
Context window detection (`model_context.py`) uses a hardcoded lookup table and API probing. For self-hosted models, the API may report incorrect defaults (e.g., llama.cpp reports whatever `-c` was set, which may be smaller than the model's actual capacity). There's no way for an admin to manually set the context window for a specific endpoint.

This causes the agent loop to truncate conversations prematurely when serving models with known context windows that differ from what the API reports.

**Proposed approach:** Add a `context_length` column to `ModelEndpoint` that overrides auto-detection. Admin can set it per endpoint in the model editor UI. Falls back to auto-detect when not set.

**Install method:** Manual Python on Linux
**Repro steps:**
1. Serve a model with `-c 4096` but the model supports 128K
2. Start a long agent conversation
3. Context gets truncated at 4096 tokens despite the model supporting more

---

### Issue C: Incorrect context window entries for recent models

**Title:** Missing or incorrect context window entries for Mistral Small 3, Qwen3.6, Qwen3-Coder

**Body:**
`KNOWN_CONTEXT_WINDOWS` in `model_context.py` is missing entries for recently released models:
- Mistral Small 3.2 (24B): supports 128K context, not in table
- Qwen3.6 (35B-A3B): supports 256K context, not in table
- Qwen3-Coder (30B-A3B): supports 256K context, not in table

The existing `mistral-small: 32000` entry matches older versions, but `mistral-small-3` doesn't match anything, falling back to `DEFAULT_CONTEXT` (128000) which is close but derived from the wrong source.

Values verified from GGUF `n_ctx_train` metadata.

**Proposed approach:** Add the three entries to `KNOWN_CONTEXT_WINDOWS`.

---

### Issue D: Docker agent sandbox for tool isolation

**Title:** Agent tool execution has no sandboxing — tools run as app user on host

**Body:**
THREAT_MODEL.md explicitly acknowledges: "No shell/filesystem sandbox. The agent bash and read_file/write_file tools run as the app process user with no network egress filtering or filesystem confinement."

A successful prompt injection on an admin session can execute arbitrary commands on the host, access internal services, and read sensitive files.

**Proposed approach:** Per-session Docker containers for agent tool isolation. Opt-in per character/session. Hardened with `--network none`, `--read-only`, `--cap-drop ALL`, `--user 1000:1000`, resource limits. Polyglot image supporting Python, Node, Rust, Go, Bun with package managers (pip, uv, npm, nvm, cargo, go mod, gh CLI). Admin UI for configuration and monitoring.

**Install method:** Manual Python on Linux with Docker

---

## Re-submission Checklist (per PR template)

For each PR, before re-submitting:

- [ ] Issue opened and linked with `Fixes #NNN`
- [ ] Summary paragraph (what and why)
- [ ] Type of Change box checked (exactly one)
- [ ] Checklist items verified
- [ ] How to Test: step-by-step instructions
- [ ] UI changes: screenshot attached, style match confirmed
- [ ] Ran `python -m pytest` — all pass
- [ ] Ran `python -m py_compile` on changed files
- [ ] Ran `node --check` on changed JS files
- [ ] Actually ran the app and verified end-to-end
- [ ] No co-authored-by or AI references in commits
- [ ] No emoji in UI or code
- [ ] Existing CSS variables/classes reused (no new styling patterns)
