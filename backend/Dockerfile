# 后端镜像：Python 3.13 + backend/requirements.lock（pip freeze 全量钉版本）
#
# ragas 怎么进来的：requirements.lock 里是
#   ragas @ git+https://github.com/GoldSharon/ragas.git@735726d5c29e072b5e4e877759d254f78e0f7c73
# 即补丁 fork 固定到 commit（版本号 0.1.dev1148+g735726d5c）。镜像里用 pip 从同一 commit 重建，
# 不拷本机 site-packages —— 否则镜像无法复现、也看不出依赖真实来源。
# 构建机需要能访问 GitHub；无外网时先在有网机器 `pip download` 或 `git clone` 后改用本地路径安装。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    RAGAS_STUDIO_DATA_DIR=/app/data

# git：pip 从 git+https 安装 ragas 需要真正的 git 可执行文件
RUN apt-get update \
 && apt-get install -y --no-install-recommends git tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只拷锁文件，依赖层可复用缓存（改业务代码不会重装依赖）
COPY requirements.lock /tmp/requirements.lock
RUN python -m pip install --upgrade pip \
 && python -m pip install -r /tmp/requirements.lock \
 && rm -f /tmp/requirements.lock

# tiktoken 编码文件构建时预取进镜像：ragas 建图（Step 1）分块计 token 时调 tiktoken.get_encoding，
# 首次使用会从公网（openaipublic.blob.core.windows.net）下载编码文件，生产实例不出公网就会失败。
# cl100k_base：ragas.utils.num_tokens_from_string 的默认值（default_transforms / HeadlineSplitter 用它）；
# o200k_base：ragas.tokenizers 的默认分词器。TIKTOKEN_CACHE_DIR 运行时同样生效，tiktoken 命中缓存不再联网。
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN python -c "import tiktoken; [tiktoken.get_encoding(n) for n in ('cl100k_base', 'o200k_base')]" \
 && ls -l /opt/tiktoken-cache

COPY . /app/backend

WORKDIR /app/backend

EXPOSE 8100

# 用自带的 urllib 做探活，镜像里不额外装 curl。
# Host 头是 127.0.0.1:8100：后端白名单始终含回环名（main.py 的 _allowed_hosts），上云只填域名也不影响探活。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/api/health', timeout=4).status == 200 else 1)"]

# 容器内必须监听 0.0.0.0 才能被同 compose 的 nginx 访问。
# 单实例约束：不要加 --workers（多进程会并发写同一份 data/ 索引）。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
