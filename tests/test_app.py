import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("WEBOPS_VERSION", raising=False)
    with TestClient(create_app()) as test_client:
        yield test_client


def test_service_and_health(client):
    assert client.get("/").json() == {"service": "webops-app"}
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_default_version(client):
    assert client.get("/version").json() == {"version": "dev"}


def test_injected_version_is_fixed_at_startup(monkeypatch):
    monkeypatch.setenv("WEBOPS_VERSION", "0123456789abcdef")
    with TestClient(create_app()) as client:
        monkeypatch.setenv("WEBOPS_VERSION", "changed-later")
        assert client.get("/version").json() == {"version": "0123456789abcdef"}


@pytest.mark.parametrize("version", ["", "with space", "line\nbreak", "x" * 129])
def test_invalid_version_rejects_startup(monkeypatch, version):
    monkeypatch.setenv("WEBOPS_VERSION", version)
    with pytest.raises(ValueError, match="WEBOPS_VERSION"):
        create_app()


def test_unknown_route_and_method(client):
    assert client.get("/missing").status_code == 404
    assert client.post("/healthz").status_code == 405


def test_unhandled_error_returns_500_without_details(monkeypatch):
    monkeypatch.delenv("WEBOPS_VERSION", raising=False)
    application = create_app()

    # Test-only route: no remotely callable failure switch in the real app.
    @application.get("/test-failure")
    def fail():
        raise RuntimeError("private-test-detail")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/test-failure")
        assert response.status_code == 500
        assert "private-test-detail" not in response.text
