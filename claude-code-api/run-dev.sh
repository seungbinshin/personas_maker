#!/usr/bin/env bash
# run-dev.sh — .env를 환경변수로 주입한 뒤 pnpm dev를 실행
# 이 래퍼 없이는 dotenv가 로드 안 돼서 API_KEYS/CLAUDE_MODEL이 undefined가 됨.
set -a
[ -f .env ] && source .env
set +a
exec pnpm dev
