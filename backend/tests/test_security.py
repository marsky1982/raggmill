"""本地服务的 Host 与浏览器写请求保护。"""
from __future__ import annotations


def test_rejects_untrusted_host():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/health", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_rejects_cross_site_write_but_allows_local_origin_to_reach_route():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        blocked = client.post(
            "/api/documents/upload",
            headers={"origin": "https://evil.example"},
        )
        allowed = client.post(
            "/api/documents/upload",
            headers={"origin": "http://localhost:5173"},
        )
    assert blocked.status_code == 403
    assert blocked.json()["error_code"] == "cross_site_request"
    # 没有 multipart 文件时由路由自身返回 422，说明同源请求通过了安全中间件。
    assert allowed.status_code == 422


def test_write_token_is_required_when_configured(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("RAGAS_STUDIO_WRITE_TOKEN", "test-token")
    with TestClient(app) as client:
        missing = client.post(
            "/api/documents/upload",
            headers={"origin": "http://127.0.0.1:5173"},
        )
        valid = client.post(
            "/api/documents/upload",
            headers={
                "origin": "http://127.0.0.1:5173",
                "x-ragas-write-token": "test-token",
            },
        )
    assert missing.status_code == 403
    assert missing.json()["error_code"] == "write_token_required"
    assert valid.status_code == 422


def test_configured_domains_extend_instead_of_replacing_loopback():
    """对外部署只填域名时，回环名仍要放行：容器健康检查按 127.0.0.1 访问，被 400 会让整个 compose 起不来。"""
    from app.main import _allowed_hosts

    hosts = _allowed_hosts("raggmill.example.com")
    assert hosts[0] == "raggmill.example.com"
    assert {"127.0.0.1", "localhost"} <= set(hosts)
    assert "evil.example" not in hosts
    # 未设置时用默认值；只填空格 / 逗号时退回默认值；大小写与重复项归一
    assert set(_allowed_hosts(None)) == {"127.0.0.1", "localhost", "testserver"}
    assert set(_allowed_hosts(" , ")) == {"127.0.0.1", "localhost", "testserver"}
    assert _allowed_hosts("Raggmill.Example.com,raggmill.example.com").count("raggmill.example.com") == 1


def test_compose_and_nginx_share_one_host_allowlist_variable():
    """后端白名单与 nginx server_name 来自同一个 STUDIO_ALLOWED_HOSTS（名字只定义一处），默认值也一致。"""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    defaults = set(re.findall(r"\$\{STUDIO_ALLOWED_HOSTS:-([^}]*)\}", compose))
    assert len(re.findall(r"STUDIO_ALLOWED_HOSTS", compose)) >= 2, "backend 与 frontend 都要引用它"
    dockerfile = (root / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    image_default = re.search(r'STUDIO_ALLOWED_HOSTS="([^"]*)"', dockerfile).group(1)
    assert defaults == {image_default}, "compose 两处默认值与前端镜像默认值必须一致"
    template = (root / "frontend" / "nginx" / "templates" / "default.conf.template").read_text(encoding="utf-8")
    assert "include /etc/nginx/studio_server_names.conf;" in template
    assert "listen 80 default_server;" in template and "return 444;" in template
    # 前端端口默认只绑回环
    assert "${STUDIO_WEB_BIND:-127.0.0.1}:${STUDIO_WEB_PORT:-8080}:80" in compose
