from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_new_client_is_allowed():
    response = client.post(
        "/v1/check",
        json={
            "client_id": "test-new-client",
            "capacity": 10,
            "refill_rate": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["allowed"] is True


def test_invalid_cost_is_rejected():
    response = client.post(
        "/v1/check",
        json={
            "client_id": "test-invalid-client",
            "cost": -1,
        },
    )

    assert response.status_code == 422