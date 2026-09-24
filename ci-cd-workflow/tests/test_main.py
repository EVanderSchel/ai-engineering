from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_greet_returns_personalized_message():
    response = client.get("/greet/Ethan")
    assert response.status_code == 200
    assert response.json() == {"message": "Hello, Ethan!"}
