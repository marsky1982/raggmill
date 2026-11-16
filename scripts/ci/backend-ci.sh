#!/bin/sh
# 后端 CI：在 Python 3.13 下跑 pytest -q。
# 前置：由 CI 在仓库根目录调用（GitHub Actions 入口见 .github/workflows/ci.yml）；
#       构建环境自带 Python 3.12 不够用（requirements 要求 3.13），这里用 uv 再装一份 CPython 3.13。
#       不需要 RAGFlow / LLM 密钥：conftest.py 已把 DATA_DIR 指到临时目录，测试全部打桩。
set -eu

export PYTHONUTF8=1
export RAGAS_DO_NOT_TRACK=true

base_py=/usr/local/python3/bin/python3
test -x "${base_py}" || base_py=python3
"${base_py}" --version

work=/tmp/ragas-ci
mkdir -p "${work}"
"${base_py}" -m pip install -q --index-url https://pypi.org/simple --target "${work}/uvtool" uv

uv="${work}/uvtool/bin/uv"
"${uv}" python install 3.13
"${uv}" venv -p 3.13 "${work}/venv"
vpy="${work}/venv/bin/python"
"${vpy}" --version

cd backend
"${uv}" pip install --python "${vpy}" --index-url https://pypi.org/simple -q -r requirements.lock
"${uv}" pip install --python "${vpy}" --index-url https://pypi.org/simple -q "$(grep -E '^pytest==' requirements-dev.txt)"
"${vpy}" -m pytest -q

echo "CI_BACKEND_OK"
