# raggmill 一键自检（Windows）：后端 pytest + 前端 lint / vitest / build。
# 前置：Python 3.13+、Node 20.19+、npm；首次需要访问 PyPI 与 npm registry。
# 0 密钥、0 外部服务；数据目录指向系统临时目录，不碰仓库里的 data/。
# 注意：本文件必须保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会按系统 ANSI 解码中文字符串并报语法错误。
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$tmp = Join-Path ([System.IO.Path]::GetTempPath) ("raggmill-verify-" + (Get-Date -Format "yyyyMMddHHmmss"))
$env:PYTHONUTF8 = "1"
$env:RAGAS_DO_NOT_TRACK = "true"
$env:RAGAS_STUDIO_DATA_DIR = $tmp

$py = Join-Path $root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[1/5] 创建后端虚拟环境并安装锁定依赖（首次较慢，需要 Python 3.13+）..."

    # ragas 与锁文件要求 Python 3.13+；逐个候选解释器试版本，第一个满足的用来建 venv
    $baseExe = $null
    $baseArgs = @()
    foreach ($cand in @(@{ exe = "py"; args = @("-3.13") }, @{ exe = "python3.13"; args = @() }, @{ exe = "python"; args = @() })) {
        if (-not (Get-Command $cand.exe -ErrorAction SilentlyContinue)) { continue }
        $ver = & $cand.exe @($cand.args + @("-c", "import sys;print('%d.%d' % sys.version_info[:2])")) 2>$null
        if ($LASTEXITCODE -eq 0 -and "$ver".Trim() -match '^3\.(1[3-9]|[2-9][0-9])$') {
            $baseExe = $cand.exe
            $baseArgs = $cand.args
            break
        }
        Write-Host ("  跳过 {0}（{1}，需要 3.13+）" -f $cand.exe, "$ver".Trim())
    }
    if (-not $baseExe) {
        Write-Host "未找到 Python 3.13+。请安装 3.13，或先用它手工建好 backend\.venv 后重跑。"
        exit 1
    }

    & $baseExe @baseArgs -m venv (Join-Path $root "backend\.venv")
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $root "backend\requirements.lock")
    & $py -m pip install -r (Join-Path $root "backend\requirements-dev.txt")
}

Write-Host "[2/5] 后端 pytest ..."
Push-Location (Join-Path $root "backend")
try { & $py -m pytest -q } finally { Pop-Location }

Push-Location (Join-Path $root "frontend")
try {
    if (-not (Test-Path "node_modules")) {
        Write-Host "[3/5] 安装前端依赖（npm ci）..."
        npm ci
    }
    Write-Host "[4/5] 前端 lint + vitest ..."
    npm run lint
    npm test
    Write-Host "[5/5] 前端构建（tsc + vite build）..."
    npm run build
} finally { Pop-Location }

Write-Host ""
Write-Host "全部通过：后端 pytest、前端 lint / vitest / build。"
