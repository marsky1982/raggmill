# 前端镜像：node 构建 → 静态产物交给 nginx
#
# 运行时不需要 Node：后端请求走同源 /api，由本镜像内的 nginx 反代到 backend 容器。
# engines 要求 node >=20.19，这里用 LTS 24。
FROM node:24-alpine AS build

WORKDIR /app

# 依赖层单独缓存：只有 package*.json 变了才重装
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:alpine

# 运行时配置由环境变量决定（compose 传入），启动时渲染模板：
#   STUDIO_ALLOWED_HOSTS  工作台域名白名单（逗号分隔，与后端同一个值）→ server_name
#   STUDIO_WRITE_TOKEN    后端写令牌，由 nginx 反代时注入请求头，不进浏览器 bundle
#   RAGFLOW_DOMAIN        RAGFlow 子域
#   RAGFLOW_WEB_UPSTREAM  RAGFlow Web 的上游地址
# 只渲染 STUDIO_ / RAGFLOW_ 开头的变量，nginx 自己的 $host 等不受影响。
ENV NGINX_ENVSUBST_FILTER="^(STUDIO_|RAGFLOW_)"     STUDIO_ALLOWED_HOSTS="127.0.0.1,localhost"     STUDIO_WRITE_TOKEN=""     RAGFLOW_DOMAIN="ragflow.example.com"     RAGFLOW_WEB_UPSTREAM="http://ragflow-cpu:80"

# 官方镜像自带的 default.conf 会抢 default_server，先删掉
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx/templates/ /etc/nginx/templates/
COPY nginx/15-studio-server-names.sh /docker-entrypoint.d/15-studio-server-names.sh
RUN chmod +x /docker-entrypoint.d/15-studio-server-names.sh
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
