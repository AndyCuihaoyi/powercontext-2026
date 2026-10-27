#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PORT="${CITEFLOW_PORT:-8848}"
if (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "CiteFlow 已在 http://127.0.0.1:$PORT 运行。"
  exit 0
fi

if [ ! -x .venv/bin/python ]; then
  echo "创建虚拟环境并安装依赖..."
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
  fi
fi

"$ROOT/scripts/start_seekdb.sh"

if [ "${CITEFLOW_START_POWERCONTEXT:-1}" = "1" ]; then
  if "$ROOT/scripts/run_powercontext.sh"; then
    export CITEFLOW_POWERCONTEXT=auto
  else
    echo "PowerContext 未启动，CiteFlow 将使用 seekdb 镜像记忆继续运行。" >&2
    export CITEFLOW_POWERCONTEXT=auto
  fi
fi

mkdir -p runtime
nohup .venv/bin/python -m app.server >runtime/server.log 2>&1 &
echo $! >runtime/server.pid

for _ in $(seq 1 40); do
  if .venv/bin/python - "$PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=1) as resp:
        raise SystemExit(0 if resp.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
  then
    echo "CiteFlow 就绪：http://127.0.0.1:$PORT"
    echo "下一步："
    echo "  .venv/bin/python scripts/seed_demo.py   # 导入样例资料（也可在页面点击“导入样例”）"
    echo "  .venv/bin/python scripts/verify_demo.py # 跑一遍端到端验证"
    exit 0
  fi
  sleep 1
done

echo "CiteFlow 启动失败，最近日志：" >&2
tail -n 30 runtime/server.log >&2 || true
exit 1
