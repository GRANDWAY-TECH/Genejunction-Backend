SERVICES_PG := identity tenant partner order payment billing pharmacy lab wholesale notification legacy_bridge
PY := $(CURDIR)/.venv/bin/python

.PHONY: venv up down logs ps test e2e migration

venv:
	python3.12 -m venv .venv
	.venv/bin/pip install -q -e libs/gj_common pytest pytest-asyncio pymongo argon2-cffi cryptography

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100 $(s)

ps:
	docker compose ps

## Unit + DB-backed tests (DB tests need `make up` for Postgres on :55432)
test:
	cd libs/gj_common && $(PY) -m pytest -q
	@for s in order catalog billing; do echo "== $$s"; (cd services/$$s && $(PY) -m pytest -q) || exit 1; done

## Full journey through the gateway (B2C, B2B2C, B2B) against the running stack
e2e:
	$(PY) scripts/e2e_journey.py

## New migration after changing models: make migration s=order m="add delivery slot"
migration:
	cd services/$(s) && DATABASE_URL=postgresql+asyncpg://gj:gj@127.0.0.1:55432/gj_$(if $(filter legacy_bridge,$(s)),bridge,$(s)) \
		$(PY) -m alembic revision --autogenerate -m "$(m)"
