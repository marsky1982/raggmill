#!/usr/bin/env bash
# raggmill 一键自检（macOS / Linux）：后端 pytest + 前端 lint / vitest / build。
# 前置：Python 3.13+、Node 20.19+、npm；首次需要访问 PyPI 与 npm registry。
# 0 密钥、0 外部服务；数据目录指向系统临时目录，不碰仓库里的 data/。
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
tmp="${TMPDIR:-/tmp}/raggmill-verify-$(date +%Y%m%d%H%M%S)"
export PYTHONUTF8=1
export RAGAS_DO_NOT_TRACK=true
export RAGAS_STUDIO_DATA_DIR="$tmp"

py="$root/backend/.venv/bin/python"
if [ ! -x "$py" ]; then
    echo "[1/5] 创建后端虚拟环境并安装锁定依赖（首次较慢，需要 Python 3.13+）..."

    # ragas 与锁文件要求 Python 3.13+；逐个候选解释器试版本，第一个满足的用来建 venv
    base=""
    for cand in python3.13 python3 python; do
        if command -v "$cand" >/dev/null 2>&1 \
            && "$cand" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 13) else 1)' 2>/dev/null; then
            base="$cand"
            break
        fi
    done
    if [ -z "$base" ]; then
        echo "未找到 Python 3.13+。请安装 3.13，或先用它手工建好 backend/.venv 后重跑。" >&2
        exit 1
    fi

    "$base" -m venv "$root/backend/.venv"
    "$py" -m pip install --upgrade pip
    "$py" -m pip install -r "$root/backend/requirements.lock"
    "$py" -m pip install -r "$root/backend/requirements-dev.txt"
fi

echo "[2/5] 后端 pytest ..."
(cd "$root/backend" && "$py" -m pytest -q)

cd "$root/frontend"
if [ ! -d node_modules ]; then
    echo "[3/5] 安装前端依赖（npm ci）..."
    npm ci
fi
echo "[4/5] 前端 lint + vitest ..."
npm run lint
npm test
echo "[5/5] 前端构建（tsc + vite build）..."
npm run build

echo
echo "全部通过：后端 pytest、前端 lint / vitest / build。"
