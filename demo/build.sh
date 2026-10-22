#!/usr/bin/env bash
# 공개 샘플 데모 빌드 한 줄: 데모 시험이 모두 통과해야 <site>를 만든다. 배포는 하지 않고 명령만 알려 준다.
set -euo pipefail
site="${1:?쓰는 법: demo/build.sh <site 폴더>}"
case $site in /*) ;; *) site="$PWD/$site" ;; esac  # 부른 폴더 기준(아래 cd 뒤에도 같은 곳)
cd "$(dirname "$0")/.."
uv run pytest -q -p no:cacheprovider tests/test_golden_pages.py tests/test_demo_sample.py tests/test_demo_static.py tests/test_demo_build.py
uv run python demo/build.py --out "$site"
find "$site/dist" -name '*.html' -exec uv run python tests/test_customer_copy.py {} +  # 만든 쪽 원문에 내부 표지가 있으면 멈춤
q=$(printf '%s' "$site/wrangler.jsonc" | sed "s/'/'\\\\''/g")  # 작은따옴표로 감싸 바이트 그대로(bash 3.2의 %q는 한글을 쪼갬)
echo "배포(BlackClip 계정 wrangler): wrangler deploy -c '$q'"
