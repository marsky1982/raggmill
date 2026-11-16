#!/bin/sh
# 把 STUDIO_ALLOWED_HOSTS（逗号分隔，与后端 RAGAS_STUDIO_ALLOWED_HOSTS 同一个值）写成 nginx 的 server_name。
# 回环名始终加上，与后端 main.py 的 _allowed_hosts 口径一致。
# nginx 官方镜像会在启动时按文件名顺序执行 /docker-entrypoint.d/*.sh；本脚本排在 20-envsubst 之前。
set -eu

names="$(printf '%s' "${STUDIO_ALLOWED_HOSTS:-}" | tr ',' ' ') 127.0.0.1 localhost"
# 只允许主机名字符，防止把奇怪内容写进 nginx 配置
clean=""
for n in $names; do
    case "$n" in
        *[!A-Za-z0-9.:_-]*) echo "15-studio-server-names: 忽略非法主机名 '$n'" >&2 ;;
        *) case " $clean " in *" $n "*) ;; *) clean="$clean $n" ;; esac ;;
    esac
done

printf 'server_name%s;\n' "$clean" > /etc/nginx/studio_server_names.conf
echo "15-studio-server-names: server_name$clean"
