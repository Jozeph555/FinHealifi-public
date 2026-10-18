"""
Тести валідації CRUD боргів.

- POST і PATCH /debt мають однакові правила полів: name 1..100 символів після
  strip, balance > 0 і <= 1e9, monthly_rate — частка 0..1, min_payment >= 0,
  усі числа скінченні (NaN/Infinity -> 422, не 500); debt_type — revolving
  або installment;
- installment потребує min_payment > 0; у PATCH — за підсумковим станом боргу;
- PATCH: явний null у полях, обов'язкових у БД, і в is_active -> 422; null у
  min_payment дозволений лише для revolving;
- чужий або неіснуючий id -> 404.

HTTP — через httpx ASGITransport на мінімальному FastAPI з роутером боргів.
"""
import json
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.api.debt as debt_module
from app.api.deps import get_current_user, get_db
from app.models.debt import Debt
from app.models.user import User


@pytest_asyncio.fixture
async def client(db_session, real_user):
    app = FastAPI()
    app.include_router(debt_module.router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: real_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


VALID_INSTALLMENT = {"name": "Розстрочка", "balance": 10_000, "monthly_rate": 0.02, "min_payment": 1_000,
                     "debt_type": "installment"}
VALID_REVOLVING = {"name": "Картка", "balance": 20_000, "monthly_rate": 0.04, "min_payment": None,
                   "debt_type": "revolving"}


async def _create(client, **body):
    resp = await client.post("/debt", json={**VALID_INSTALLMENT, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _raw(client, method, url, body: str):
    # NaN/Infinity не серіалізуються стандартним json=, тому тіло — сирий рядок.
    return client.request(method, url, content=body, headers={"content-type": "application/json"})


def _error_fields(resp) -> set:
    return {e["loc"][-1] for e in resp.json()["detail"] if isinstance(e, dict) and e.get("loc")}


# --- власник / існування ---------------------------------------------------------------

async def test_patch_unknown_id_is_404(client):
    resp = await client.patch(f"/debt/{uuid.uuid4()}", json={"balance": 5_000})

    assert resp.status_code == 404


async def test_patch_foreign_debt_is_404_and_unchanged(db_session, client):
    other = User(id=f"test-{uuid.uuid4()}", email=f"other-{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(other)
    await db_session.flush()
    foreign = Debt(id=str(uuid.uuid4()), user_id=other.id, name="Чужий", balance=7_000.0, monthly_rate=0.01,
                   min_payment=500.0, debt_type="installment", is_active=True)
    db_session.add(foreign)
    await db_session.flush()

    resp = await client.patch(f"/debt/{foreign.id}", json={"balance": 1})

    assert resp.status_code == 404
    await db_session.refresh(foreign)
    assert foreign.balance == 7_000.0


# --- діапазони й скінченність ----------------------------------------------------------

BAD_VALUES = [
    ("balance", 0), ("balance", -1), ("balance", 1e9 + 1),
    ("monthly_rate", -0.01), ("monthly_rate", 1.01),
    ("min_payment", -1),
    ("name", ""), ("name", "   "), ("name", "x" * 101),
]


@pytest.mark.parametrize("field,value", BAD_VALUES)
async def test_post_rejects_out_of_range(client, field, value):
    resp = await client.post("/debt", json={**VALID_INSTALLMENT, field: value})

    assert resp.status_code == 422
    assert field in _error_fields(resp)


@pytest.mark.parametrize("field,value", BAD_VALUES)
async def test_patch_rejects_out_of_range(client, field, value):
    debt = await _create(client)

    resp = await client.patch(f"/debt/{debt['id']}", json={field: value})

    assert resp.status_code == 422
    assert field in _error_fields(resp)


@pytest.mark.parametrize("field", ["balance", "monthly_rate", "min_payment"])
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
async def test_post_non_finite_is_422(client, field, raw):
    body = {**VALID_INSTALLMENT}
    body.pop(field)
    text = json.dumps(body)[:-1] + f', "{field}": {raw}}}'

    resp = await _raw(client, "POST", "/debt", text)

    assert resp.status_code == 422
    assert field in _error_fields(resp)


@pytest.mark.parametrize("field", ["balance", "monthly_rate", "min_payment"])
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
async def test_patch_non_finite_is_422(client, field, raw):
    debt = await _create(client)

    resp = await _raw(client, "PATCH", f"/debt/{debt['id']}", '{"%s": %s}' % (field, raw))

    assert resp.status_code == 422
    assert field in _error_fields(resp)


async def test_revolving_negative_min_payment_is_422(client):
    # для installment від'ємний мінімум ловить і правило «> 0»; тут — лише ge=0
    resp = await client.post("/debt", json={**VALID_REVOLVING, "min_payment": -1})
    assert resp.status_code == 422
    assert "min_payment" in _error_fields(resp)

    debt = await _create(client, **VALID_REVOLVING)
    resp = await client.patch(f"/debt/{debt['id']}", json={"min_payment": -1})
    assert resp.status_code == 422
    assert "min_payment" in _error_fields(resp)


async def test_name_is_stripped(client):
    debt = await _create(client, name="  Автокредит  ")
    assert debt["name"] == "Автокредит"

    resp = await client.patch(f"/debt/{debt['id']}", json={"name": "  Іпотека "})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Іпотека"


async def test_boundaries_are_accepted(client):
    debt = await _create(client, balance=1e9, monthly_rate=1.0)
    assert debt["balance"] == 1e9 and debt["monthly_rate"] == 1.0

    resp = await client.patch(f"/debt/{debt['id']}", json={"monthly_rate": 0, "name": "x" * 100})
    assert resp.status_code == 200


# --- debt_type ---------------------------------------------------------------------------

async def test_post_invalid_debt_type_is_422(client):
    resp = await client.post("/debt", json={**VALID_INSTALLMENT, "debt_type": "mortgage"})

    assert resp.status_code == 422


async def test_patch_invalid_debt_type_is_422_and_unchanged(client):
    debt = await _create(client)

    resp = await client.patch(f"/debt/{debt['id']}", json={"debt_type": "mortgage"})

    assert resp.status_code == 422
    assert "debt_type" in _error_fields(resp)
    listed = (await client.get("/debt")).json()
    assert [d["debt_type"] for d in listed if d["id"] == debt["id"]] == ["installment"]


# --- null у PATCH ------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["name", "balance", "monthly_rate", "debt_type", "is_active"])
async def test_patch_null_in_required_field_is_422(client, field):
    debt = await _create(client)

    resp = await client.patch(f"/debt/{debt['id']}", json={field: None})

    assert resp.status_code == 422
    assert field in _error_fields(resp)
    listed = (await client.get("/debt")).json()
    assert [d["id"] for d in listed] == [debt["id"]]  # борг і далі активний і читається


async def test_patch_null_min_payment_ok_for_revolving(client):
    debt = await _create(client, **{**VALID_REVOLVING, "min_payment": 300})

    resp = await client.patch(f"/debt/{debt['id']}", json={"min_payment": None})

    assert resp.status_code == 200
    assert resp.json()["min_payment"] is None


async def test_patch_null_min_payment_rejected_for_installment(client):
    debt = await _create(client)

    resp = await client.patch(f"/debt/{debt['id']}", json={"min_payment": None})

    assert resp.status_code == 422
    assert "min_payment" in _error_fields(resp)


# --- installment потребує min_payment > 0 ------------------------------------------------

@pytest.mark.parametrize("min_payment", [None, 0])
async def test_post_installment_without_min_payment_is_422(client, min_payment):
    resp = await client.post("/debt", json={**VALID_INSTALLMENT, "min_payment": min_payment})

    assert resp.status_code == 422
    assert "min_payment" in _error_fields(resp)


async def test_post_installment_missing_min_payment_key_is_422(client):
    body = {k: v for k, v in VALID_INSTALLMENT.items() if k != "min_payment"}

    resp = await client.post("/debt", json=body)

    assert resp.status_code == 422


async def test_patch_to_installment_without_min_payment_is_422(client):
    debt = await _create(client, **VALID_REVOLVING)

    resp = await client.patch(f"/debt/{debt['id']}", json={"debt_type": "installment"})

    assert resp.status_code == 422
    assert "min_payment" in _error_fields(resp)


async def test_patch_to_installment_with_min_payment_ok(client):
    debt = await _create(client, **VALID_REVOLVING)

    resp = await client.patch(f"/debt/{debt['id']}", json={"debt_type": "installment", "min_payment": 800})

    assert resp.status_code == 200
    assert resp.json()["debt_type"] == "installment" and resp.json()["min_payment"] == 800


async def test_patch_installment_zero_min_payment_is_422(client):
    debt = await _create(client)

    resp = await client.patch(f"/debt/{debt['id']}", json={"min_payment": 0})

    assert resp.status_code == 422


async def test_post_revolving_with_null_min_payment_ok(client):
    debt = await _create(client, **VALID_REVOLVING)

    assert debt["min_payment"] is None and debt["debt_type"] == "revolving"


# --- зв'язок з планом і список -----------------------------------------------------------

async def test_patch_balance_makes_plan_stale(client):
    debt = await _create(client)
    assert (await client.post("/debt/optimize", json={"extra_monthly_uah": 1_000})).status_code == 201
    assert (await client.get("/debt/plan/latest")).json()["is_stale"] is False

    assert (await client.patch(f"/debt/{debt['id']}", json={"balance": 9_000})).status_code == 200

    assert (await client.get("/debt/plan/latest")).json()["is_stale"] is True


async def test_list_include_inactive_returns_repaid(client):
    active = await _create(client, name="Активний")
    repaid = await _create(client, name="Погашений")
    assert (await client.patch(f"/debt/{repaid['id']}", json={"is_active": False})).status_code == 200

    default_ids = {d["id"] for d in (await client.get("/debt")).json()}
    all_debts = {d["id"]: d for d in (await client.get("/debt", params={"include_inactive": True})).json()}

    assert default_ids == {active["id"]}
    assert set(all_debts) == {active["id"], repaid["id"]}
    assert all_debts[repaid["id"]]["is_active"] is False
