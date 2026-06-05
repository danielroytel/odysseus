# Docker Agent Sandbox Specification

**Goal:** Isolate agent tool execution (bash, python, file ops) in per-session Docker containers with full shell access and zero host exposure.

**Why:** Agent tools currently run as the app process user on the host. A successful prompt injection gives an attacker access to internal services, credentials, and the filesystem. This is explicitly acknowledged in `THREAT_MODEL.md:75`.

**Design Principles:**
- Opt-in per character/session (off by default, no breaking changes)
- Fail-closed (refuse execution if sandbox is requested but unavailable)
- Per-session containers (started on first tool call, reused across calls)
- Full workspace access inside container, zero host access outside workspace
- Admin-configurable: image, resource limits, network policy, idle timeout
- Compatible with PR #751 (optional-sandbox), PR #1429 (workspace-cwd), PR #221 (file-tools)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Odysseus Application (host)                                │
│                                                             │
│  ┌──────────────┐     ┌─────────────────────────────────┐   │
│  │ agent_loop.py │────▶│ sandbox_manager.py              │   │
│  │              │     │                                 │   │
│  │ tool calls   │     │ get_or_create(session_id)       │   │
│  │              │     │   ├─ check active containers    │   │
│  │              │     │   ├─ create if none exists      │   │
│  │              │     │   └─ return container handle    │   │
│  │              │     │                                 │   │
│  │              │     │ exec(container, command)         │   │
│  │              │     │   └─ docker exec ...             │   │
│  │              │     │                                 │   │
│  │              │     │ put_file(container, path, data)  │   │
│  │              │     │ get_file(container, path)        │   │
│  │              │     │                                 │   │
│  │              │     │ cleanup(session_id)              │   │
│  │              │     │   └─ docker stop + rm            │   │
│  └──────────────┘     └──────────┬──────────────────────┘   │
│                                  │                           │
│  ┌──────────────────────────────┐│                           │
│  │ settings.py                  ││                           │
│  │ sandbox_enabled: bool        ││                           │
│  │ sandbox_image: str           ││                           │
│  │ sandbox_memory: str          ││                           │
│  │ sandbox_cpus: float          ││                           │
│  │ sandbox_network: bool        ││                           │
│  │ sandbox_idle_timeout: int    ││                           │
│  └──────────────────────────────┘│                           │
│                                  │ docker CLI / API          │
└──────────────────────────────────┼───────────────────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │  Docker Container           │
                    │                             │
                    │  /workspace  ←── bind mount │
                    │    (read-write)             │
                    │                             │
                    │  python3, bash, git, etc.   │
                    │  (from sandbox image)       │
                    │                             │
                    │  --network none (default)   │
                    │  --memory 2g                │
                    │  --cpus 2                   │
                    │  --pids-limit 256           │
                    │  --read-only (rootfs)       │
                    │  --cap-drop ALL             │
                    │  --user 1000:1000           │
                    └─────────────────────────────┘
```

---

## Component: `src/sandbox.py` (new file)

### SandboxManager class

```python
class SandboxManager:
    """Manages per-session Docker containers for agent tool isolation."""

    def __init__(self):
        self._containers: Dict[str, SandboxContainer] = {}
        self._runtime: Optional[str] = None  # "docker" or "podman"
        self._lock = asyncio.Lock()

    async def detect_runtime(self) -> str:
        """Detect available container runtime. Prefers podman, falls back to docker."""

    async def get_or_create(
        self,
        session_id: str,
        workspace_dir: str,
        config: SandboxConfig,
    ) -> SandboxContainer:
        """Get existing container for session, or create a new one.

        If a container exists but config changed (different image),
        destroy and recreate.
        """

    async def exec(
        self,
        session_id: str,
        command: str,
        timeout: int = 3600,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, str, str]:
        """Execute command inside session's container.

        Returns: (exit_code, stdout, stderr)
        """

    async def read_file(self, session_id: str, path: str) -> str:
        """Read file from container workspace."""

    async def write_file(self, session_id: str, path: str, content: str) -> None:
        """Write file into container workspace."""

    async def cleanup(self, session_id: str) -> None:
        """Stop and remove container for session."""

    async def cleanup_idle(self) -> int:
        """Stop containers idle beyond timeout. Returns count cleaned."""

    async def cleanup_all(self) -> int:
        """Stop and remove all managed containers. Used on shutdown."""
```

### SandboxContainer dataclass

```python
@dataclass
class SandboxContainer:
    container_id: str
    session_id: str
    workspace_dir: str       # host path
    container_workspace: str  # container path (always /workspace)
    image: str
    created_at: float
    last_used_at: float
    config: SandboxConfig
```

### SandboxConfig dataclass

```python
@dataclass
class SandboxConfig:
    image: str = "odysseus/sandbox:latest"
    memory: str = "2g"
    cpus: float = 2.0
    pids_limit: int = 256
    tmpfs_size: str = "512m"
    network: bool = False     # False = --network none
    idle_timeout: int = 1800  # seconds, stop after 30 min idle
    extra_bind_mounts: List[str] = field(default_factory=list)
```

### Container lifecycle

**Creation** (on first tool call for a sandboxed session):

```bash
docker run -d \
  --name odysseus-sandbox-{session_id} \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  --tmpfs /home/sandbox:rw,nosuid,nodev,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --user 1000:1000 \
  --memory 4g \
  --cpus 2 \
  --pids-limit 256 \
  -v {workspace_dir}:/workspace:rw \
  -v {gitconfig}:/home/sandbox/.gitconfig:ro \
  -v {gh_config}:/home/sandbox/.config/gh:ro \
  -e GITHUB_TOKEN={github_token} \
  -w /workspace \
  odysseus/sandbox:latest \
  sleep infinity
```

**Execution** (per tool call):

```bash
docker exec \
  --user 1000:1000 \
  --workdir /workspace \
  odysseus-sandbox-{session_id} \
  bash -c "{command}"
```

**File reads** (avoid bind-mount host access):

```bash
docker exec odysseus-sandbox-{session_id} cat /workspace/{relative_path}
```

**File writes** (stream via stdin to handle large files):

```bash
echo '{base64_content}' | base64 -d | docker cp - odysseus-sandbox-{session_id}:/workspace/{relative_path}
```

**Idle cleanup** (periodic task):

```bash
docker stop odysseus-sandbox-{session_id}
docker rm odysseus-sandbox-{session_id}
```

---

## Component: `src/tool_execution.py` modifications

### Integration point

The sandbox wraps the existing subprocess calls in `execute_tool_block()`. When sandboxing is enabled for a session:

1. **bash tool**: Replace `asyncio.create_subprocess_shell()` with `sandbox_manager.exec()`
2. **python tool**: Replace `asyncio.create_subprocess_exec()` with `sandbox_manager.exec()` using `python3 -I -c "{code}"`
3. **read_file**: Replace direct `open()` with `sandbox_manager.read_file()`
4. **write_file**: Replace direct `open()` with `sandbox_manager.write_file()`
5. **edit_file**: Read via sandbox, apply edit in-memory, write via sandbox
6. **glob**: Replace `pathlib` / `rg` with `sandbox_manager.exec("rg --files ...")`
7. **grep**: Replace `rg` subprocess with `sandbox_manager.exec("rg ...")`

### Session sandbox state

Add to the agent loop's session context:

```python
# In stream_agent_loop():
sandbox_enabled: bool = character_config.get("sandbox", False)
sandbox_manager: Optional[SandboxManager] = app_state.get("sandbox_manager")
```

Pass `sandbox_enabled` and `session_id` into `execute_tool_block()`:

```python
result = await execute_tool_block(
    tool_name=tool_name,
    content=content,
    session_id=session_id,
    sandbox_enabled=sandbox_enabled,
    sandbox_manager=sandbox_manager,
    ...
)
```

---

## Component: Settings and Configuration

### Application settings (`src/settings.py`)

```python
# Sandbox defaults (overridable via admin UI)
SANDBOX_DEFAULTS = {
    "image": "odysseus/sandbox:latest",
    "memory": "4g",
    "cpus": 2.0,
    "pids_limit": 256,
    "tmpfs_size": "512m",
    "network": False,
    "idle_timeout": 1800,
}
```

### Per-character sandbox toggle

Add to character configuration:

```python
# In character JSON config or DB:
{
    "sandbox": true,           # enable sandboxing for this character
    "sandbox_image": null,     # override default image
    "sandbox_network": false   # override default network policy
}
```

### Admin UI

Add sandbox section to Settings panel:

- **Enable sandbox** (global toggle, default off)
- **Container image** (text input, default `odysseus/sandbox:latest`)
- **Memory limit** (dropdown: 2g, 4g, 8g, 16g)
- **CPU limit** (dropdown: 1, 2, 4)
- **Network access** (toggle, default off)
- **Idle timeout** (dropdown: 15m, 30m, 1h, 4h)
- **Test button**: Creates a test container, runs `uname -a`, shows result

### Character editor

Add sandbox toggle per character:

- **Sandbox mode** (checkbox: enable container isolation)
- **Allow network** (checkbox, only shown when sandbox enabled)
- **Custom image** (text input, optional override)

---

## Component: Startup and Shutdown Hooks

### Application startup (`main.py`)

```python
# In lifespan/startup:
sandbox_enabled = get_setting("sandbox_enabled", False)
if sandbox_enabled:
    from src.sandbox import SandboxManager
    app.state.sandbox_manager = SandboxManager()
    await app.state.sandbox_manager.detect_runtime()
    logger.info(f"Sandbox manager initialized (runtime: {app.state.sandbox_manager._runtime})")
```

### Periodic idle cleanup

```python
# In background task (existing periodic tasks pattern):
async def _sandbox_idle_cleanup():
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        manager = app.state.get("sandbox_manager")
        if manager:
            cleaned = await manager.cleanup_idle()
            if cleaned:
                logger.info(f"Cleaned up {cleaned} idle sandbox containers")
```

### Application shutdown

```python
# In lifespan/shutdown:
manager = app.state.get("sandbox_manager")
if manager:
    cleaned = await manager.cleanup_all()
    logger.info(f"Shut down {cleaned} sandbox containers")
```

---

## Component: Database

### No schema changes needed

Sandbox state is ephemeral (containers are created/stopped at runtime). Configuration lives in:
- `settings` table (global defaults)
- Character configs (per-character overrides)

Container IDs are tracked in-memory only (`SandboxManager._containers`). On crash/restart, orphaned containers are cleaned up by matching the `odysseus-sandbox-` name prefix.

---

## Security Design

### Container hardening

| Layer | Mechanism | Purpose |
|-------|-----------|---------|
| Network | `--network none` | Prevent exfiltration, lateral movement |
| Filesystem | `--read-only` + tmpfs | Immutable rootfs, writable only in /tmp and /workspace |
| Capabilities | `--cap-drop ALL` | No Linux capabilities |
| Privileges | `--security-opt no-new-privileges` | No SUID escalation |
| User | `--user 1000:1000` | Non-root inside container |
| Resources | `--memory`, `--cpus`, `--pids-limit` | Prevent resource exhaustion |
| PID | `--pids-limit 256` | Prevent fork bombs |

### Path confinement

All file operations are translated to container-relative paths:

```
Host: /home/user/odysseus/data/workspaces/project-x/
Container: /workspace/

read_file("/workspace/src/main.py")
  → docker exec ... cat /workspace/src/main.py

write_file("/workspace/src/new.py", content)
  → echo BASE64 | base64 -d | docker cp - CONTAINER:/workspace/src/new.py
```

The host's `_resolve_tool_path()` is bypassed entirely for sandboxed sessions. Path validation happens inside the container (the container has no access outside `/workspace`).

### Fail-closed behavior

```python
async def exec(self, session_id, command, ...):
    container = self._containers.get(session_id)
    if not container:
        raise SandboxError(f"No sandbox container for session {session_id}")

    # Verify container is still running
    try:
        result = await self._docker_cmd(
            ["inspect", "--format", "{{.State.Running}}", container.container_id]
        )
        if result.strip() != "true":
            raise SandboxError(f"Container {container.container_id} is not running")
    except Exception as e:
        # Remove stale reference
        self._containers.pop(session_id, None)
        raise SandboxError(f"Container health check failed: {e}")

    # Execute command
    ...
```

If the container runtime is unavailable when sandboxing is requested:
- Refuse the tool execution (return error to LLM)
- Log the failure
- Do NOT fall back to host execution (unless `ODYSSEUS_SANDBOX_FALLBACK=host` is explicitly set)

### Container escape mitigation

- **No privileged mode**: Never use `--privileged`
- **No new privileges**: `no-new-privileges` prevents SUID escalation
- **Minimal image**: Default image is `odysseus/sandbox:latest` (polyglot, all languages)
- **No Docker socket**: Container never gets access to `/var/run/docker.sock`
- **User namespaces**: Podman uses user namespaces by default; Docker can be configured with `--userns-keep-id`

---

## Container Image Strategy

### Default image: `odysseus/sandbox:latest` (polyglot)

A single purpose-built image supporting all major development languages.

### Polyglot Sandbox Image (`Dockerfile.sandbox`)

```dockerfile
# Multi-stage: install each toolchain, then squash into final image
# to keep size manageable.

# ---- Stage 1: Python ----
FROM python:3.12-slim AS python-stage

# ---- Stage 2: Node.js ----
FROM node:22-slim AS node-stage

# ---- Stage 3: Go ----
FROM golang:1.23-alpine AS go-stage

# ---- Final: Polyglot Sandbox ----
FROM python:3.12-slim

# --- System packages ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget jq ripgrep fd-find bat build-essential pkg-config \
    libssl-dev ca-certificates gnupg unzip && \
    rm -rf /var/lib/apt/lists/*

# --- Node.js (from node-stage) ---
COPY --from=node-stage /usr/local/bin/node /usr/local/bin/node
COPY --from=node-stage /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack

# --- nvm (Node Version Manager) ---
# Install nvm so agents can install arbitrary Node versions
ENV NVM_DIR=/opt/nvm
RUN mkdir -p $NVM_DIR && \
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash \
    && . "$NVM_DIR/nvm.sh" && nvm install-latest-npm
# nvm available via: . /opt/nvm/nvm.sh && nvm use <version>

# --- uv (Python package manager) ---
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# uv replaces pip for speed: uv pip install, uv venv, uv run
# pip still available from base Python image

# --- Go (from go-stage) ---
COPY --from=go-stage /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}"

# --- Bun (JavaScript runtime, alternative to Node) ---
RUN curl -fsSL https://bun.sh/install | bash -s -- -y
ENV PATH="/root/.bun/bin:${PATH}"

# --- GitHub CLI (gh) ---
RUN ARCH=$(dpkg --print-architecture) && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# --- Create non-root user ---
RUN useradd -m -u 1000 sandbox && \
    mkdir -p /home/sandbox/.cargo /home/sandbox/.rustup \
              /home/sandbox/.local /home/sandbox/.nvm \
              /home/sandbox/.bun && \
    chown -R sandbox:sandbox /home/sandbox

# --- User-level toolchains ---
USER sandbox
WORKDIR /workspace

# Rust (via rustup for sandbox user)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
ENV PATH="/home/sandbox/.cargo/bin:${PATH}"

# nvm for sandbox user (allows agent to switch Node versions)
ENV NVM_DIR=/home/sandbox/.nvm
RUN mkdir -p $NVM_DIR && \
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash

# uv configuration for sandbox user
ENV UV_CACHE_DIR=/home/sandbox/.cache/uv

# --- Verify all toolchains ---
RUN python3 --version && pip --version && uv --version \
    && node --version && npm --version && npx --version \
    && go version && rustc --version && cargo --version \
    && git --version && rg --version && jq --version
```

**Package managers available inside the container:**

| Manager | Language | Usage |
|---------|----------|-------|
| `pip` | Python | `pip install <package>` |
| `uv` | Python | `uv pip install`, `uv venv`, `uv run` (10-100x faster than pip) |
| `npm` | Node.js | `npm install <package>` |
| `npx` | Node.js | `npx <command>` (run without installing) |
| `nvm` | Node.js | `. ~/.nvm/nvm.sh && nvm install 20` (switch Node versions) |
| `corepack` | Node.js | `corepack enable` (pnpm, yarn) |
| `bun` | JavaScript | `bun install`, `bun run` (alternative runtime) |
| `cargo` | Rust | `cargo build`, `cargo add <crate>` |
| `rustup` | Rust | `rustup toolchain install nightly` (switch Rust versions) |
| `go mod` | Go | `go mod tidy`, `go get <pkg>` |
| `gh` | GitHub | `gh pr create`, `gh issue list`, `gh repo clone` (auth via workspace token) |
| `apt-get` | System | Only during image build (rootfs is read-only at runtime) |

**Important:** At runtime, the rootfs is `--read-only`. Package installation goes to:
- Python: `uv pip install --user <pkg>` → `/home/sandbox/.local/lib/python3.12/`
- Node: `npm install <pkg>` → `/workspace/node_modules/` (in workspace, persisted)
- Rust: `cargo add <crate>` → `/workspace/Cargo.toml` (in workspace)
- Go: `go get <pkg>` → `/workspace/go.mod` (in workspace)
- System: Not available at runtime (use workspace Dockerfile for system deps)

### Credential passthrough

Selected host credentials can be mounted read-only into the container:

```bash
# In container creation, if credential passthrough is enabled:
-v /home/user/.gitconfig:/home/sandbox/.gitconfig:ro   # git identity
-v /home/user/.config/gh:/home/sandbox/.config/gh:ro    # gh auth token
-e GITHUB_TOKEN=${GITHUB_TOKEN}                          # env-based auth
```

Configuration in settings:
```python
SANDBOX_CREDENTIAL_PASSTHROUGH = {
    "git": True,       # mount .gitconfig
    "gh": True,        # mount gh config + token
    "ssh": False,      # mount .ssh (off by default — sensitive)
}
```

This allows the agent to use `gh pr create`, `git push`, etc. inside the sandbox without manual auth.

**Resulting image includes:**
| Tool | Version | For |
|------|---------|-----|
| Python 3.12 | pip, uv, venv | Python development |
| Node.js 22 | npm, npx, nvm, corepack, bun | JavaScript/TypeScript |
| Rust stable | cargo, rustc, rustup | Rust development |
| Go 1.23 | go, gofmt | Go development |
| Git | | Version control |
| ripgrep (rg) | | Fast search |
| jq | | JSON processing |
| curl/wget | | HTTP clients |
| build-essential | gcc, make | C/C++ compilation |
| GitHub CLI | gh | PRs, issues, repos, API |
| fd-find | fd | Fast file finding |
| bat | bat | Better cat |

**Image size estimate:** ~1.8GB (compressed ~600MB)

### Layered Image Strategy

Not all sessions need all languages. Odysseus supports three image modes:

**1. Polyglot (default):** `odysseus/sandbox:latest` — all languages
**2. Language-specific:** Lightweight images per language
**3. Workspace Dockerfile:** Custom image defined in workspace

```python
SANDBOX_IMAGES = {
    "polyglot": "odysseus/sandbox:latest",       # All languages (~1.8GB)
    "python":   "python:3.12-slim",               # Python only (~150MB)
    "node":     "node:22-slim",                    # Node.js only (~250MB)
    "rust":     "rust:1-slim",                     # Rust only (~1.5GB)
    "go":       "golang:1.23-alpine",              # Go only (~300MB)
    "minimal":  "alpine:3.20",                     # Shell only (~8MB)
}
```

### Workspace Dockerfile

If a workspace contains `.sandbox/Dockerfile`, Odysseus builds a custom image from it on first use:

```
workspace/
├── .sandbox/
│   ├── Dockerfile          # Custom sandbox image
│   └── .dockerignore
├── src/
└── ...
```

```python
async def _ensure_workspace_image(self, workspace_dir: str) -> str:
    """Build custom sandbox image from workspace Dockerfile if present."""
    dockerfile = os.path.join(workspace_dir, ".sandbox", "Dockerfile")
    if not os.path.exists(dockerfile):
        return None

    image_tag = f"odysseus-workspace-{hash(workspace_dir)}:latest"
    result = await self._docker_cmd([
        "build", "-t", image_tag,
        "-f", dockerfile,
        os.path.join(workspace_dir, ".sandbox")
    ])
    return image_tag
```

This allows per-project dependency installation (e.g., a Python project can include `requirements.txt` in the Dockerfile for pre-installed packages).

### Custom images via admin

Admin can specify any image in settings or per-character config. Odysseus will:
1. Pull the image if not available locally
2. Verify the image has a working shell (`/bin/sh`)
3. Detect available toolchains by running `which python3 node go rustc 2>/dev/null`
4. Log available tools (warn if python tool is used but python not found)

### Image pre-pull

On startup, pull configured images in background:

```python
async def _prepull_images(self):
    """Pull sandbox images in background to avoid cold-start delays."""
    images = set([self._default_image])
    for char in get_all_characters():
        if char.get("sandbox_image"):
            images.add(char["sandbox_image"])
    for image in images:
        asyncio.create_task(self._docker_cmd(["pull", image]))
```

---

## Streaming and Progress

### Streaming command output

`docker exec` stdout/stderr is captured via `asyncio.create_subprocess_exec`:

```python
proc = await asyncio.create_subprocess_exec(
    self._runtime, "exec",
    container.container_id,
    "bash", "-c", command,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)

# Stream with progress callback (same pattern as current tool_execution.py)
```

### Progress events

Reuse the existing `progress_cb` mechanism:

```python
async def exec(self, session_id, command, timeout=3600, progress_cb=None):
    # Same streaming pattern as _run_bash() in tool_execution.py
    # Stream stdout lines, call progress_cb every PROGRESS_INTERVAL_S
    # Tail buffer for last N lines
```

---

## Orphan Cleanup

On startup, clean up any containers from a previous crash:

```python
async def cleanup_orphans(self):
    """Remove any odysseus-sandbox-* containers from previous sessions."""
    result = await self._docker_cmd([
        "ps", "-a", "--filter", "name=odysseus-sandbox-",
        "--format", "{{.ID}} {{.Names}}"
    ])
    count = 0
    for line in result.strip().splitlines():
        cid, name = line.split(maxsplit=1)
        await self._docker_cmd(["stop", cid])
        await self._docker_cmd(["rm", cid])
        count += 1
    if count:
        logger.info(f"Cleaned up {count} orphaned sandbox containers")
    return count
```

---

## API Endpoints

### `GET /api/sandbox/status`

Returns sandbox manager status:

```json
{
    "enabled": true,
    "runtime": "docker",
    "active_containers": 3,
    "containers": [
        {
            "session_id": "abc123",
            "image": "python:3.12-slim",
            "created_at": "2026-06-03T10:00:00Z",
            "last_used_at": "2026-06-03T10:05:00Z",
            "workspace_dir": "/home/user/data/workspaces/project-x"
        }
    ]
}
```

### `POST /api/sandbox/cleanup`

Force cleanup of idle containers:

```json
{
    "cleaned": 2
}
```

### `POST /api/sandbox/test`

Test sandbox creation (admin only):

```json
{
    "success": true,
    "output": "Linux abc123 6.1.0 ... x86_64 GNU/Linux",
    "container_id": "short-hash",
    "cleaned_up": true
}
```

---

## Migration Path

### Phase 1: Core sandbox (this spec)

- `src/sandbox.py` (new): SandboxManager, SandboxContainer, SandboxConfig
- `src/tool_execution.py`: Add sandbox routing in `execute_tool_block()`
- `src/settings.py`: Add sandbox configuration defaults
- `main.py`: Startup/shutdown hooks, idle cleanup task
- `THREAT_MODEL.md`: Update with sandbox coverage

### Phase 2: Admin UI

- Settings panel: Sandbox configuration section
- Character editor: Per-character sandbox toggle
- `/api/sandbox/status`: Monitoring endpoint
- `/api/sandbox/test`: Test endpoint

### Phase 3: Advanced features

- Custom image builder (from `Dockerfile.sandbox` in workspace)
- Network egress allowlist (specific domains only)
- Multi-container sessions (e.g., app + database)
- Container checkpointing (pause/resume long sessions)

---

## Testing Strategy

### Unit tests (`tests/test_sandbox.py`)

- `test_detect_runtime`: Mock subprocess, verify docker/podman detection
- `test_create_container`: Verify correct docker run flags
- `test_exec_command`: Verify docker exec with correct args
- `test_cleanup_container`: Verify stop + rm
- `test_cleanup_idle`: Verify idle timeout enforcement
- `test_cleanup_orphans`: Verify name-prefix matching
- `test_read_write_file`: Verify file operations through container
- `test_fail_closed`: Verify error when runtime unavailable
- `test_config_validation`: Verify SandboxConfig defaults and constraints

### Integration tests (require Docker)

- `test_full_lifecycle`: Create → exec → read → write → cleanup
- `test_path_confinement`: Verify no access outside /workspace
- `test_network_isolation`: Verify `--network none` blocks curl
- `test_resource_limits`: Verify memory/CPU limits enforced
- `test_idle_timeout`: Verify container stopped after timeout
- `test_crash_recovery`: Verify orphan cleanup on restart

### Test markers

```python
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ODYSSEUS_TEST_DOCKER"),
    reason="Requires Docker (set ODYSSEUS_TEST_DOCKER=1)"
)
```

---

## Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `src/sandbox.py` | Create | SandboxManager, SandboxContainer, SandboxConfig |
| `src/tool_execution.py` | Modify | Add sandbox routing in execute_tool_block() |
| `src/settings.py` | Modify | Add SANDBOX_DEFAULTS |
| `main.py` | Modify | Startup/shutdown hooks, idle cleanup task |
| `routes/sandbox_routes.py` | Create | /api/sandbox/* endpoints |
| `THREAT_MODEL.md` | Modify | Update with sandbox coverage |
| `Dockerfile.sandbox` | Create | Pre-built sandbox image |
| `static/js/admin.js` | Modify | Sandbox settings UI |
| `tests/test_sandbox.py` | Create | Unit and integration tests |

---

## Compatibility with Upstream PRs

| PR | Relationship |
|----|-------------|
| #751 (optional-sandbox) | **Supersedes**. PR #751 creates a per-command container with no bind mounts. This spec provides per-session containers with workspace mounts — more capable and performant. |
| #1429 (workspace-cwd) | **Compatible**. Sandbox uses the same workspace resolution. Container mount path matches `resolve_workspace_dir()`. |
| #221 (file-tools-parity) | **Compatible**. File tools (edit_file, glob, grep) are routed through the sandbox when enabled. |
| #1058 (issue, closed) | **Addresses**. This spec implements the sandboxing that issue #1058 requested. |
