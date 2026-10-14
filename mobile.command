#!/bin/zsh
set -eu
project_dir="${0:A:h}"
cd "$project_dir"
if [[ ! -x "$project_dir/.venv/bin/ecourse" ]]; then
  print '請先依 README 安裝 Python 依賴（uv sync --locked）。'
  exit 1
fi
export ECOURSE_DATA_DIR="${ECOURSE_DATA_DIR:-$project_dir/.data}"
export ECOURSE_SECRET_DIR="${ECOURSE_SECRET_DIR:-$project_dir/.secrets}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$project_dir/.cache/ms-playwright}"
exec "$project_dir/.venv/bin/ecourse" mobile --browser default
