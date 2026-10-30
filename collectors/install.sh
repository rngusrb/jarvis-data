#!/bin/bash
# 맥 수집기 셋(chrome, apps, forward)을 launchd 에 올린다. 다시 돌려도 안전하다.
#
#   sh collectors/install.sh            # 올리기 (이미 있으면 새로 올린다)
#   sh collectors/install.sh --remove   # 내리기
#
# 토큰은 plist 에 적지 않는다. 먼저 상태 폴더에 둔다:
#   mkdir -p ~/Library/Application\ Support/jarvis
#   printf '%s' '<토큰>' > ~/Library/Application\ Support/jarvis/token
#   chmod 600 ~/Library/Application\ Support/jarvis/token
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/jarvis"
STATE="$HOME/Library/Application Support/jarvis"
SERVER="${JARVIS_SERVER:-http://100.98.90.38:8100}"
DOMAIN="gui/$(id -u)"
NAMES="chrome apps forward"

stop() {
  launchctl bootout "$DOMAIN/ai.jarvis.$1" 2>/dev/null || true
  # bootout 은 바로 안 끝난다. 끝나기 전에 bootstrap 하면 5번 오류가 난다.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$DOMAIN/ai.jarvis.$1" >/dev/null 2>&1 || return 0
    sleep 0.5
  done
}

if [ "${1:-}" = "--remove" ]; then
  for name in $NAMES; do
    stop "$name"
    rm -f "$AGENTS/ai.jarvis.$name.plist"
    echo "내림: ai.jarvis.$name"
  done
  exit 0
fi

if [ ! -s "$STATE/token" ]; then
  echo "토큰이 없다: $STATE/token 에 먼저 둔다 (이 파일 맨 위 설명)" >&2
  exit 1
fi

mkdir -p "$AGENTS" "$LOGS"
for name in $NAMES; do
  target="$AGENTS/ai.jarvis.$name.plist"
  sed -e "s#__REPO__#$REPO#g" -e "s#__LOGS__#$LOGS#g" -e "s#__SERVER__#$SERVER#g" \
    "$REPO/collectors/launchd/ai.jarvis.$name.plist" > "$target"
  stop "$name"
  launchctl bootstrap "$DOMAIN" "$target"
  echo "올림: ai.jarvis.$name"
done
echo "로그: $LOGS  (tail -f $LOGS/*.log)"
