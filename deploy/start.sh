#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${DASHSCOPE_API_KEY:?请先 export DASHSCOPE_API_KEY}"
export DASHSCOPE_BASE_URL="${DASHSCOPE_BASE_URL:-https://ws-ebzun9rcecl8l3rh.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1}"
mkdir -p results state
project_dir="$PWD"
worker_name="llm-performance-worker-1"
runtime_image="llm-performance-runtime:local"
storage_args=()
if [[ -f state/oss.env ]]; then
  storage_args=(--env-file "$project_dir/state/oss.env")
fi
docker build --target runtime-base -t "$runtime_image" .
docker run --rm -v "$project_dir:/workspace" -w /workspace \
  -e TIKTOKEN_CACHE_DIR=/workspace/.token-cache \
  "$runtime_image" python deploy/bootstrap.py
if docker container inspect "$worker_name" >/dev/null 2>&1; then
  docker stop --time 120 "$worker_name"
  docker rm "$worker_name"
fi
docker run -d --name "$worker_name" --restart unless-stopped \
  "${storage_args[@]}" \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /tmp \
  -p 8001:8001 \
  -v "$project_dir:/workspace:ro" \
  -v "$project_dir/results:/workspace/results" \
  -v "$project_dir/state:/workspace/state" \
  -w /workspace -e DASHSCOPE_API_KEY -e DASHSCOPE_BASE_URL \
  -e PYTHONUNBUFFERED=1 -e PYTHONDONTWRITEBYTECODE=1 \
  "$runtime_image" .runtime/bin/python deploy/worker.py
python3 - <<'PY'
import time
import urllib.request
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8001/healthz',timeout=2) as response:
            if response.status == 200:
                print('服务已启动：http://127.0.0.1:8001')
                break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit('健康检查未通过，请运行 docker logs --tail=50 llm-performance-worker-1')
PY
printf '%s\n' '工作台无需登录，直接访问 8001 端口。'
