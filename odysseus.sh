#!/usr/bin/env bash
set -euo pipefail

NAME="odysseus"
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/$NAME.pid"
LOGFILE="$DIR/$NAME.log"
DOCKER="${DOCKER:-docker}"

# Load .env overrides if present
if [[ -f "$DIR/.env" ]]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        value="${value#\"}" ; value="${value%\"}"
        value="${value#\'}" ; value="${value%\'}"
        export "$key=$value"
    done < "$DIR/.env"
fi

HOST="${APP_HOST:-${HOST:-0.0.0.0}}"
PORT="${APP_PORT:-${PORT:-7000}}"

CHROMADB_HOST="${CHROMADB_HOST:-127.0.0.1}"
CHROMADB_PORT="${CHROMADB_PORT:-8100}"
CHROMADB_BIND="${CHROMADB_BIND:-127.0.0.1}"
SEARXNG_BIND="${SEARXNG_BIND:-127.0.0.1}"
NTFY_BIND="${NTFY_BIND:-127.0.0.1}"
NTFY_BASE_URL="${NTFY_BASE_URL:-http://localhost:8091}"

# ── local LLM model server ──
LLAMA_SERVER="${LLAMA_SERVER:-$(command -v llama-server 2>/dev/null || true)}"
LLM_PORT="${LLM_PORT:-8000}"
LLM_CTX="${LLM_CTX:-131072}"
LLM_NGL="${LLM_NGL:-99}"
LLM_ALIAS="${LLM_ALIAS:-}"
LLM_MODEL="${LLM_MODEL:-}"  # explicit path; if empty, auto-detects from HF cache

# KV cache & attention tuning
LLM_FLASH_ATTN="${LLM_FLASH_ATTN:-auto}"         # auto|on|off — flash attention
LLM_CACHE_TYPE_K="${LLM_CACHE_TYPE_K:-q8_0}"      # f32|f16|bf16|q8_0|q5_1 — KV cache K dtype
LLM_CACHE_TYPE_V="${LLM_CACHE_TYPE_V:-q5_1}"      # f32|f16|bf16|q8_0|q5_1 — KV cache V dtype
LLM_FIT="${LLM_FIT:-}"                            # "on" to auto-shrink ctx to fit VRAM (unset = use full ctx with KV spill to RAM)

# Auto-detect GGUF model from HuggingFace cache
_llm_find_gguf() {
    local cache="$HOME/.cache/huggingface/hub"
    [[ -d "$cache" ]] || return 1
    # Prefer models with a snapshots/main/*.gguf symlink (complete downloads)
    for model_dir in "$cache"/models--*/; do
        [[ -d "$model_dir" ]] || continue
        for gguf in "$model_dir"/snapshots/main/*.gguf; do
            if [[ -L "$gguf" ]]; then
                echo "$gguf"
                return 0
            fi
        done
    done
    return 1
}

# ── helpers ──────────────────────────────────────────────────

log()  { echo "[$NAME] $*"; }

pids() {
    pgrep -f "uvicorn app:app --host $HOST --port $PORT" 2>/dev/null || true
    pgrep -f "$DIR/mcp_servers/.*_server\.py" 2>/dev/null || true
}

need_docker() {
    if ! command -v "$DOCKER" &>/dev/null; then
        log "ERROR: docker not found. Install Docker or run services manually."
        return 1
    fi
    if ! $DOCKER info &>/dev/null; then
        log "ERROR: Cannot connect to Docker daemon. Try: sudo systemctl start docker"
        return 1
    fi
}

docker_running() {
    local name="$1"
    $DOCKER ps --format '{{.Names}}' 2>/dev/null | grep -qx "$name"
}

# ── dependency services ─────────────────────────────────────

start_deps() {
    need_docker || return 1

    # Quick check: can Docker actually run containers? (catches veth/bridge issues)
    if ! $DOCKER run --rm docker.io/hello-world &>/dev/null; then
        log "WARNING: Docker can't create containers (bridge networking broken?). Skipping deps."
        log "Hint: try 'sudo modprobe veth' or reboot into a kernel with matching modules."
        return 1
    fi

    # ChromaDB
    if docker_running odysseus-chromadb; then
        log "ChromaDB already running."
    else
        log "Starting ChromaDB on ${CHROMADB_BIND}:${CHROMADB_PORT} ..."
        $DOCKER rm -f odysseus-chromadb &>/dev/null || true
        $DOCKER run -d \
            --name odysseus-chromadb \
            -p "${CHROMADB_BIND}:${CHROMADB_PORT}:8000" \
            -v "${DIR}/data/chroma:/chroma/chroma" \
            -e ANONYMIZED_TELEMETRY=FALSE \
            --restart unless-stopped \
            docker.io/chromadb/chroma:latest
        # Brief wait for ChromaDB to accept connections
        for _ in $(seq 1 10); do
            curl -sf "http://localhost:${CHROMADB_PORT}/api/v1/heartbeat" &>/dev/null && break
            sleep 1
        done
    fi

    # SearXNG
    if docker_running odysseus-searxng; then
        log "SearXNG already running."
    else
        log "Starting SearXNG on ${SEARXNG_BIND}:8080 ..."
        $DOCKER rm -f odysseus-searxng &>/dev/null || true

        # Generate secret if not set
        local secret="${SEARXNG_SECRET:-}"
        if [[ -z "$secret" ]]; then
            secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))' 2>/dev/null || echo "fallback-$(date +%s)")"
        fi

        # Render settings from template
        local tmp_settings
        tmp_settings="$(mktemp)"
        sed "s|__SEARXNG_SECRET__|${secret}|g" "$DIR/config/searxng/settings.yml" > "$tmp_settings"

        $DOCKER run -d \
            --name odysseus-searxng \
            -p "${SEARXNG_BIND}:8080:8080" \
            -v "$tmp_settings:/etc/searxng/settings.yml:ro" \
            -e SEARXNG_BASE_URL=http://localhost:8080/ \
            -e SEARXNG_SECRET="$secret" \
            --restart unless-stopped \
            docker.io/searxng/searxng:latest
    fi

    # ntfy
    if docker_running odysseus-ntfy; then
        log "ntfy already running."
    else
        log "Starting ntfy on ${NTFY_BIND}:8091 ..."
        $DOCKER rm -f odysseus-ntfy &>/dev/null || true
        $DOCKER run -d \
            --name odysseus-ntfy \
            -p "${NTFY_BIND}:8091:80" \
            -e NTFY_BASE_URL="$NTFY_BASE_URL" \
            --restart unless-stopped \
            docker.io/binwiederhier/ntfy serve
    fi
}

stop_deps() {
    need_docker || return 1
    for svc in odysseus-ntfy odysseus-searxng odysseus-chromadb; do
        if docker_running "$svc"; then
            log "Stopping $svc ..."
            $DOCKER stop "$svc" &>/dev/null || true
            $DOCKER rm -f "$svc" &>/dev/null || true
        fi
    done
}

# ── local LLM model server ──────────────────────────────────

_llm_pid() {
    pgrep -f "llama-server.*--port $LLM_PORT" 2>/dev/null || true
}

start_llm() {
    if [[ -n "$(_llm_pid)" ]]; then
        log "Local LLM already running on port $LLM_PORT."
        return 0
    fi

    if [[ -z "$LLAMA_SERVER" ]]; then
        log "WARNING: llama-server not found; skipping local LLM."
        return 0
    fi

    # Resolve model path
    local model="$LLM_MODEL"
    if [[ -z "$model" ]]; then
        model="$(_llm_find_gguf)" || true
        if [[ -z "$model" ]]; then
            log "WARNING: No GGUF model found in HF cache; skipping local LLM."
            return 0
        fi
    fi

    if [[ ! -e "$model" ]]; then
        log "WARNING: Model not found: $model; skipping local LLM."
        return 0
    fi

    # Derive a clean alias from the model path
    local alias="$LLM_ALIAS"
    if [[ -z "$alias" ]]; then
        # Extract model name from HF cache path: models--org--ModelName-GGUF/.../file.gguf
        # Produces: modelname (lowercase, no org, no -gguf/-ggml suffix)
        if [[ "$model" == *"/hub/models--"* ]]; then
            local repo
            repo="$(echo "$model" | sed 's|.*/models--||;s|/snapshots.*||')"
            # Strip org prefix (everything before second --) and -GGUF/-GGML suffix
            alias="$(echo "$repo" | sed 's/^[^-]*--//' | sed 's/-gguf//Ig; s/-ggml//Ig' | tr '[:upper:]' '[:lower:]' | tr '_' '-' | tr -d '/')"
        else
            alias="$(basename "$model" .gguf | tr '[:upper:]' '[:lower:]')"
        fi
    fi

    log "Starting local LLM: $(basename "$model") on port $LLM_PORT (ctx=$LLM_CTX, ngl=$LLM_NGL, cache-k=$LLM_CACHE_TYPE_K, cache-v=$LLM_CACHE_TYPE_V) ..."
    local cmd=(
        "$LLAMA_SERVER"
        -m "$model"
        --host 0.0.0.0
        --port "$LLM_PORT"
        --alias "$alias"
        -c "$LLM_CTX"
        -ngl "$LLM_NGL"
        --flash-attn "$LLM_FLASH_ATTN"
        --cache-type-k "$LLM_CACHE_TYPE_K"
        --cache-type-v "$LLM_CACHE_TYPE_V"
    )
    [[ -n "$LLM_FIT" ]] && cmd+=(--fit "$LLM_FIT")
    nohup "${cmd[@]}" >> "$DIR/llm.log" 2>&1 &

    # Wait for server to be ready (model loading can take a while)
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:$LLM_PORT/v1/models" &>/dev/null; then
            log "Local LLM ready — model '$alias' on port $LLM_PORT"
            return 0
        fi
        sleep 2
    done

    log "WARNING: Local LLM did not become ready within 60s. Check $DIR/llm.log"
}

stop_llm() {
    local pid
    pid="$(_llm_pid)"
    if [[ -z "$pid" ]]; then
        return 0
    fi
    log "Stopping local LLM ..."
    echo "$pid" | xargs kill 2>/dev/null || true
    for _ in $(seq 1 5); do
        [[ -z "$(_llm_pid)" ]] && break
        sleep 1
    done
}

deps_status() {
    if ! command -v "$DOCKER" &>/dev/null; then
        echo "  docker: not installed"
    else
        for svc in odysseus-chromadb odysseus-searxng odysseus-ntfy; do
            if docker_running "$svc"; then
                echo "  $svc: running"
            else
                echo "  $svc: stopped"
            fi
        done
    fi
    if [[ -n "$(_llm_pid)" ]]; then
        echo "  local-llm: running (port $LLM_PORT)"
    else
        echo "  local-llm: stopped"
    fi
}

# ── main app ────────────────────────────────────────────────

do_kill() {
    stop_llm
    local found
    found=$(pids)
    if [[ -z "$found" ]]; then
        log "Not running."
        return 0
    fi
    log "Stopping..."
    echo "$found" | xargs kill 2>/dev/null || true

    for _ in $(seq 1 10); do
        [[ -z $(pids) ]] && break
        sleep 1
    done

    remaining=$(pids)
    if [[ -n "$remaining" ]]; then
        echo "$remaining" | xargs kill -9 2>/dev/null || true
    fi

    rm -f "$PIDFILE"
    log "Stopped."
}

do_start() {
    if [[ -n $(pids) ]]; then
        log "Already running. Use '$0 restart' to restart."
        return 1
    fi

    start_deps || log "WARNING: Docker deps skipped (Docker unavailable). ChromaDB/SearXNG/ntfy will not start."
    start_llm

    log "Starting on http://$HOST:$PORT ..."
    cd "$DIR"
    source venv/bin/activate
    nohup python -m uvicorn app:app --host "$HOST" --port "$PORT" >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"

    for _ in $(seq 1 5); do
        if curl -s "http://localhost:$PORT" > /dev/null 2>&1; then
            log "Ready — http://localhost:$PORT"
            log "Log: tail -f $LOGFILE"
            return 0
        fi
        sleep 1
    done

    log "Started (PID $(cat "$PIDFILE")). Check log: tail -f $LOGFILE"
}

# ── commands ────────────────────────────────────────────────

case "${1:-}" in
    start)
        do_start
        ;;
    stop)
        do_kill
        ;;
    restart)
        do_kill
        do_start
        ;;
    deps-start)
        start_deps
        start_llm
        ;;
    deps-stop)
        stop_llm
        stop_deps
        ;;
    llm-start)
        start_llm
        ;;
    llm-stop)
        stop_llm
        ;;
    status)
        if [[ -n $(pids) ]]; then
            log "Running — $(pids | tr '\n' ' ')"
        else
            log "Not running."
        fi
        deps_status
        ;;
    log)
        tail -f "$LOGFILE"
        ;;
    llm-log)
        tail -f "$DIR/llm.log"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|log|deps-start|deps-stop|llm-start|llm-stop|llm-log}"
        echo ""
        echo "  start        Start deps (ChromaDB, SearXNG, ntfy, local LLM) then Odysseus"
        echo "  stop         Stop local LLM and Odysseus"
        echo "  restart      Stop then start"
        echo "  deps-start   Start Docker deps + local LLM without Odysseus"
        echo "  deps-stop    Stop local LLM + Docker deps"
        echo "  llm-start    Start local LLM model server only"
        echo "  llm-stop     Stop local LLM model server"
        echo "  status       Show Odysseus + dependency + LLM status"
        echo "  log          Tail the Odysseus log"
        echo "  llm-log      Tail the local LLM log"
        echo ""
        echo "Environment variables:"
        echo "  LLM_MODEL    Path to GGUF model (auto-detected from HF cache if empty)"
        echo "  LLM_PORT     LLM server port (default: 8000)"
        echo "  LLM_CTX      Context length (default: 8192)"
        echo "  LLM_NGL      GPU layers (default: 99)"
        echo "  LLM_ALIAS       Model alias (default: derived from filename)"
        echo "  LLM_FLASH_ATTN  Flash attention: auto|on|off (default: auto)"
        echo "  LLM_CACHE_TYPE_K  KV cache K dtype (default: q8_0)"
        echo "  LLM_CACHE_TYPE_V  KV cache V dtype (default: q5_1)"
        echo "  LLM_FIT         Set 'on' to auto-shrink ctx to fit VRAM"
        exit 1
        ;;
esac
