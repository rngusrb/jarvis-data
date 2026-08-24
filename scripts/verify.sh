#!/bin/sh
# 완료 검증 단일 명령. CI 가 보는 것과 같은 것을 본다.
set -e
python scripts/harness.py all --gc
