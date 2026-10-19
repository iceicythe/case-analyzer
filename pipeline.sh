#!/usr/bin/env bash
# Case Analyzer 常用流水线：test / check / import / perf / serve / docker。
# 用法：./pipeline.sh <命令> [参数]
#   ./pipeline.sh test                跑单元测试与仓库自检
#   ./pipeline.sh check               只跑仓库自检（不跑测试）
#   ./pipeline.sh import <目录>       批量导入（其余参数原样透传给 python -m app import）
#   ./pipeline.sh perf [--rows 100000] [--cases 4]   生成合成数据并测导入吞吐
#   ./pipeline.sh serve [--port 8770] 启动工作台
#   ./pipeline.sh docker              构建并启动容器
set -euo pipefail

PY="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

case "${1:-}" in
  test)
    "$PY" -B -X utf8 -m unittest discover -s tests -v
    "$PY" scripts/check_repo.py
    ;;
  check)
    "$PY" scripts/check_repo.py
    ;;
  import)
    shift
    "$PY" -B -X utf8 -m app import "$@"
    ;;
  perf)
    shift
    "$PY" -B -X utf8 scripts/measure_throughput.py "$@"
    ;;
  serve)
    shift
    exec "$PY" -B -X utf8 -m app "$@"
    ;;
  docker)
    docker compose up --build
    ;;
  *)
    sed -n '2,12p' "$0"
    exit 2
    ;;
esac
