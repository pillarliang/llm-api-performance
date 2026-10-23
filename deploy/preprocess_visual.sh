#!/usr/bin/env bash
# Independent preprocessing container; does not restart the benchmark worker.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${VISUAL_OSS_ENV_FILE:-$project_dir/state/oss.env}"
work_dir="${VISUAL_WORK_DIR:-$project_dir/state/visual-preprocess}"
if [[ ! -f "$env_file" ]]; then
  echo "缺少 OSS 凭证文件：$env_file" >&2
  exit 1
fi
mkdir -p "$work_dir"
docker build --target visual-preprocess -t llm-performance-visual:local "$project_dir"
exec docker run --rm --init --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --env-file "$env_file" \
  --mount "type=bind,source=$work_dir,target=/work" \
  -e TMPDIR=/work \
  llm-performance-visual:local --work-dir /work "$@"
