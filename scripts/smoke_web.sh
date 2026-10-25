#!/usr/bin/env bash
# Web 全栈冒烟(M9 W8,opt-in):docker compose 起栈 → curl 契约冒烟。
# 需要真实 Docker;CI 不跑(见 WHYCODE_SMOKE_REAL 守卫)。
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${WHYCODE_SMOKE_REAL:-}" != "1" ]]; then
  echo "跳过:Web 冒烟需显式 WHYCODE_SMOKE_REAL=1(docker compose 起栈,CI 不跑)。"
  exit 0
fi

echo "[1/3] 构建并起栈..."
docker compose up --build -d
trap 'docker compose down' EXIT

echo "[2/3] curl 契约冒烟..."
uv run python scripts/smoke_web_curl.py

echo "[3/3] (可选)浏览器冒烟 — 如需真实点击允许/拒绝,手动跑:"
echo "      WHYCODE_SMOKE_REAL=1 uv run python scripts/smoke_browser.py"
echo "✓ Web 全栈冒烟通过。"
