#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${CITEFLOW_SEEKDB_PORT:-2891}"
BASE_DIR="${CITEFLOW_SEEKDB_BASE_DIR:-$ROOT/runtime/seekdb}"
PID_FILE="$BASE_DIR/seekdb.pid"
LOG_FILE="$BASE_DIR/seekdb.out"

if (echo >"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo "seekdb 已在 127.0.0.1:$PORT 监听，跳过启动。"
  exit 0
fi

SEEKDB_BIN="${SEEKDB_BIN:-}"
if [ -z "$SEEKDB_BIN" ]; then
  SEEKDB_BIN="$(command -v seekdb || true)"
fi
if [ -z "$SEEKDB_BIN" ]; then
  for candidate in \
    "$HOME/OceanBase/work/seekdb-install/root/usr/bin/seekdb" \
    "$HOME/seekdb/bin/seekdb" \
    "/usr/local/bin/seekdb"; do
    if [ -x "$candidate" ]; then
      SEEKDB_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$SEEKDB_BIN" ] || [ ! -x "$SEEKDB_BIN" ]; then
  cat >&2 <<'EOF'
未找到 seekdb 可执行文件。
请先安装 seekdb 1.4.x，然后二选一：
  SEEKDB_BIN=/path/to/seekdb scripts/start_seekdb.sh
  export PATH=/path/to/seekdb/bin:$PATH
EOF
  exit 1
fi

mkdir -p "$BASE_DIR/store"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "seekdb 进程已存在（PID $(cat "$PID_FILE")），但端口 $PORT 尚未就绪。"
  echo "日志：$LOG_FILE"
  exit 1
fi

echo "启动 seekdb：$SEEKDB_BIN"
nohup "$SEEKDB_BIN" \
  --nodaemon \
  --base-dir="$BASE_DIR" \
  --port="$PORT" \
  --data-dir="$BASE_DIR/store" \
  --redo-dir="$BASE_DIR/store/redo" \
  --parameter memory_limit="${SEEKDB_MEMORY_LIMIT:-4G}" \
  --parameter datafile_size="${SEEKDB_DATAFILE_SIZE:-2G}" \
  --parameter log_disk_size="${SEEKDB_LOG_DISK_SIZE:-2G}" \
  >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"
for _ in $(seq 1 60); do
  if "$PYTHON" - "$PORT" <<'PY' >/dev/null 2>&1
import sys
try:
    import pymysql
except ImportError:
    import socket
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1).close()
    raise SystemExit(0)
try:
    conn = pymysql.connect(host="127.0.0.1", port=int(sys.argv[1]), user="root",
                           password="", connect_timeout=1)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
    conn.close()
except Exception:
    raise SystemExit(1)
PY
  then
    echo "seekdb 就绪：127.0.0.1:$PORT（PID $(cat "$PID_FILE")）"
    echo "日志：$LOG_FILE"
    exit 0
  fi
  if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "seekdb 进程已退出，最近日志：" >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "seekdb 未在 60 秒内就绪。最近日志：" >&2
tail -n 30 "$LOG_FILE" >&2 || true
exit 1
