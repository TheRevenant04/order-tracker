import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def insert_order(order_id, created_at, priority="express"):
    with main.connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, "Sam", "Headphones", priority, "preparing", created_at),
        )
    return order_id


# Pinned inputs rather than datetime.now(), so these run on every day of the
# year and are not a guard only on the two days a month the old
# replace(day=...) arithmetic happened to break. `2026-08-31` is the
# express-1002 row init_db() seeded during the 2026-09-29 incident;
# `2026-09-29` is the express order POST /api/orders created in the same
# incident.
@pytest.mark.parametrize(
    "created_at, estimated_delivery",
    [
        ("2026-09-15T10:00:00+00:00", "2026-09-17"),
        ("2026-08-31T09:00:00+00:00", "2026-09-02"),
        ("2026-09-29T12:00:00+00:00", "2026-10-01"),
        ("2026-09-30T23:59:59+00:00", "2026-10-02"),
        ("2026-12-31T00:00:00+00:00", "2027-01-02"),
        ("2026-02-27T00:00:00+00:00", "2026-03-01"),
        ("2024-02-28T00:00:00+00:00", "2024-03-01"),
    ],
)
def test_express_estimated_delivery_rolls_over_boundaries(created_at, estimated_delivery):
    row = {
        "id": "express-month-end",
        "customer": "Sam",
        "item": "Headphones",
        "priority": "express",
        "status": "preparing",
        "created_at": created_at,
    }
    assert main.order_detail(row)["estimated_delivery"] == estimated_delivery


def test_standard_order_has_no_estimated_delivery():
    row = {
        "id": "standard-month-end",
        "customer": "Avery",
        "item": "Notebook",
        "priority": "standard",
        "status": "received",
        "created_at": "2026-09-29T12:00:00+00:00",
    }
    assert "estimated_delivery" not in main.order_detail(row)


def test_get_express_order_placed_at_month_end(client):
    order_id = insert_order("express-get", "2026-08-31T09:00:00+00:00")

    response = client.get(f"/api/orders/{order_id}")

    assert response.status_code == 200
    assert response.json()["estimated_delivery"] == "2026-09-02"


def test_patch_express_order_placed_at_month_end(client):
    order_id = insert_order("express-patch", "2026-09-29T12:00:00+00:00")

    response = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})

    assert response.status_code == 200
    assert response.json()["estimated_delivery"] == "2026-10-01"
