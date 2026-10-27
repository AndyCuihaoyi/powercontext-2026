#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${CITEFLOW_POWERCONTEXT_PORT:-17429}"
RUNTIME="$ROOT/runtime/powercontext"
ENV_FILE="$RUNTIME/.env"
LOG_FILE="$RUNTIME/server.log"

if (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "PowerContext 已在 127.0.0.1:$PORT 监听，跳过启动。"
  exit 0
fi

PC_BIN="${POWERCONTEXT_BIN:-}"
if [ -z "$PC_BIN" ]; then
  PC_BIN="$(command -v powercontext || true)"
fi
if [ -z "$PC_BIN" ] || [ ! -x "$PC_BIN" ]; then
  cat >&2 <<'EOF'
未找到 powercontext 命令。安装示例（需要 Python 3.11+）：
  uv tool install powercontext
  # 或 pipx install powercontext
安装后重试，或设置 POWERCONTEXT_BIN=/path/to/powercontext。
若不使用 PowerContext，可跳过此脚本；CiteFlow 会自动降级到 seekdb 镜像记忆。
EOF
  exit 1
fi

mkdir -p "$RUNTIME"
if [ ! -f "$ENV_FILE" ]; then
  echo "生成 PowerContext 基础配置（无模型 Key 模式）..."
  printf 'y\n' | "$PC_BIN" config init --template --output "$ENV_FILE" >/dev/null
  sed -i.bak "s/^POWERCONTEXT_SERVER_HTTP_PORT=.*/POWERCONTEXT_SERVER_HTTP_PORT=$PORT/" "$ENV_FILE"
  sed -i.bak "s#127.0.0.1:8000#127.0.0.1:$PORT#g" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
fi

if grep -q '^POWERCONTEXT_SERVER_HTTP_PORT=' "$ENV_FILE"; then
  sed -i.bak "s/^POWERCONTEXT_SERVER_HTTP_PORT=.*/POWERCONTEXT_SERVER_HTTP_PORT=$PORT/" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
fi

echo "启动 PowerContext：$PC_BIN"
nohup "$PC_BIN" server run --env-file "$ENV_FILE" >"$LOG_FILE" 2>&1 &
echo $! >"$RUNTIME/server.pid"

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
for _ in $(seq 1 40); do
  if "$PYTHON" - "$PORT" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/health/ready", timeout=1) as resp:
        payload = json.load(resp)
    raise SystemExit(0 if payload.get("status") == "ready" else 1)
except Exception:
    raise SystemExit(1)
PY
  then
    echo "PowerContext 就绪：http://127.0.0.1:$PORT（PID $(cat "$RUNTIME/server.pid")）"
    echo "配置：$ENV_FILE"
    echo "日志：$LOG_FILE"
    exit 0
  fi
  if ! kill -0 "$(cat "$RUNTIME/server.pid")" 2>/dev/null; then
    echo "PowerContext 进程已退出，最近日志：" >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "PowerContext 未在 40 秒内就绪。最近日志：" >&2
tail -n 30 "$LOG_FILE" >&2 || true
exit 1
