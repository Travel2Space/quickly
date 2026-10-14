from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security import SecurityHeadersMiddleware


def test_csp_allows_only_self_scripts():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/x")
    def x():
        return {"ok": True}

    csp = TestClient(app).get("/x").headers["content-security-policy"]
    assert "script-src 'self';" in csp
    assert "connect-src 'self';" in csp
    assert "unpkg.com" not in csp and "cloudflareinsights" not in csp
