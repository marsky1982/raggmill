#!/bin/sh
# 前端 CI：npm ci + ESLint + Vitest + 类型检查与构建。
# 前置：由 CI 在仓库根目录调用（GitHub Actions 入口见 .github/workflows/ci.yml）；
#       这一步用默认镜像，不自带 Node，所以从 nodejs.org 取 latest-v22.x（engines 要求 >=20.19.0）。
set -eu

work=/tmp/ragas-ci-node
mkdir -p "${work}"

case "$(uname -m)" in
  x86_64) node_arch=x64 ;;
  aarch64|arm64) node_arch=arm64 ;;
  *) echo "CI_NODE_ARCH_UNSUPPORTED: $(uname -m)" >&2; exit 1 ;;
esac

dist_base=https://nodejs.org/dist/latest-v22.x
curl -fsSL --retry 2 --connect-timeout 20 --max-time 120 "${dist_base}/SHASUMS256.txt" -o "${work}/SHASUMS256.txt"

line=$(grep -E "node-v[0-9.]+-linux-${node_arch}[.]tar[.]gz$" "${work}/SHASUMS256.txt" || true)
if [ -z "${line}" ]; then
  echo "CI_NODE_SHASUMS_MISSING: ${node_arch}" >&2
  exit 1
fi

sha=$(printf '%s\n' "${line}" | awk '{print $1}')
tarball=$(printf '%s\n' "${line}" | awk '{print $2}')
curl -fsSL --retry 2 --connect-timeout 20 --max-time 300 "${dist_base}/${tarball}" -o "${work}/node.tar.gz"
printf '%s  %s\n' "${sha}" "${work}/node.tar.gz" | sha256sum -c -

mkdir -p "${work}/node"
tar -xzf "${work}/node.tar.gz" -C "${work}/node" --strip-components=1
PATH="${work}/node/bin:${PATH}"
export PATH
node --version
npm --version

cd frontend
npm ci
npm run lint
npm test
npm run build

echo "CI_FRONTEND_OK"
