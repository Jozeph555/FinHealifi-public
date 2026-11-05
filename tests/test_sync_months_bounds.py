"""
Межі глибини історії в POST /mono/sync/start: months від 1 до 24.

Запити йдуть у мінімальний FastAPI-застосунок із роутером mono. Рахунку з
переданим id не існує, тож запит, що пройшов валідацію, закінчується 404 і
фонова синхронізація не запускається.
"""
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.api.mono as mono_module
from app.api.deps import get_current_user, get_db

MISSING_ACCOUNT = "немає-такого-рахунку"


@pytest_asyncio.fixture
async def client(db_session, real_user):
    app = FastAPI()
    app.include_router(mono_module.router)

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: real_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.mark.parametrize("months", [0, 25, 99])
async def test_months_out_of_range_rejected(client, months):
    resp = await client.post("/mono/sync/start", json={"account_ids": [MISSING_ACCOUNT], "months": months})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "months"]


@pytest.mark.parametrize("months", [1, 12, 24])
async def test_months_in_range_passes_validation(client, months):
    resp = await client.post("/mono/sync/start", json={"account_ids": [MISSING_ACCOUNT], "months": months})
    assert resp.status_code == 404, resp.text
