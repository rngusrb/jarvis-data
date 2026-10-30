#!/bin/bash
# 맥 수집기 셋(chrome, apps, forward)을 launchd 에 올린다. 다시 돌려도 안전하다 — 업데이트도 이걸로 한다.
#
#   sh collectors/install.sh            # 올리기 / 머지된 최신으로 바꾸기
#   sh collectors/install.sh --remove   # 내리기
#
# 수집기는 이 저장소가 아니라 상태 폴더의 사본(app/)에서 돈다. 이 사본은 origin 의 기본
# 브랜치만 받는다. 개발하던 저장소에서 돌리면 브랜치를 바꾸거나 고치는 도중에 정시 실행이
# 걸려, 반쯤 고친 코드가 커서를 옮긴다(2026-10-30 설치 첫날 그 상태로 한 시간 넘게 돌았다).
#
# 토큰은 plist 에 적지 않는다. 먼저 상태 폴더에 둔다:
#   mkdir -p ~/Library/Application\ Support/jarvis
#   printf '%s' '<토큰>' > ~/Library/Application\ Support/jarvis/token
#   chmod 600 ~/Library/Application\ Support/jarvis/token
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$HOME/Library/Application Support/jarvis"
LIVE="$STATE/app"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/jarvis"
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

if [ ! -d "$LIVE/.git" ]; then
  git clone --quiet "$(git -C "$HERE" remote get-url origin)" "$LIVE"
fi
# 사본은 받기만 하는 곳이다. 거기서 고친 파일이 있으면 pull 이 조용히 성공하고 고친 게
# 그대로 돈다 — 찍히는 해시와 실제 코드가 달라진다.
if [ -n "$(git -C "$LIVE" status --porcelain --untracked-files=no)" ]; then
  echo "사본에 고친 파일이 있다: $LIVE" >&2
  git -C "$LIVE" status --short --untracked-files=no >&2
  echo "고칠 건 저장소에서 PR 로. 사본은 git -C \"$LIVE\" checkout . 으로 되돌린다" >&2
  exit 1
fi
git -C "$LIVE" pull --quiet --ff-only
echo "수집기 코드: $(git -C "$LIVE" log --oneline -1)"

mkdir -p "$AGENTS" "$LOGS"
for name in $NAMES; do
  target="$AGENTS/ai.jarvis.$name.plist"
  sed -e "s#__REPO__#$LIVE#g" -e "s#__LOGS__#$LOGS#g" -e "s#__SERVER__#$SERVER#g" \
    "$LIVE/collectors/launchd/ai.jarvis.$name.plist" > "$target"
  stop "$name"
  launchctl bootstrap "$DOMAIN" "$target"
  echo "올림: ai.jarvis.$name"
done
echo "로그: $LOGS  (tail -f $LOGS/*.log)"
