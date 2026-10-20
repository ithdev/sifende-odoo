DB ?= sifende_odoo
TEST_TAGS ?= /l10n_py_edi_sifende
ODOO = docker compose run --rm --no-deps -T odoo odoo
ADDONS = --addons-path=/mnt/oca/l10n-paraguay,/mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons

E2E_DB ?= sifende_e2e
E2E_ENV = -e SIFENDE_E2E_API_KEY -e SIFENDE_E2E_BASE_URL -e SIFENDE_E2E_RUC -e SIFENDE_E2E_STAMP \
	-e SIFENDE_E2E_FIRST_NUMBER -e SIFENDE_E2E_CASES -e SIFENDE_E2E_DEV_API_KEY
E2E_SHELL = docker compose run --rm --no-deps -T $(E2E_ENV) odoo odoo shell $(ADDONS) -d $(E2E_DB) --log-level=warn

.PHONY: help fetch-oca build up down test shell run e2e-reset e2e-setup e2e-cases e2e-run

help:
	@echo "make fetch-oca  clona OCA/l10n-paraguay (pineado) en vendor/"
	@echo "make build      construye la imagen de Odoo con las dependencias"
	@echo "make test       instala el módulo en una base nueva y corre sus tests"
	@echo "make run        levanta Odoo en http://localhost:$${SIFENDE_ODOO_PORT:-8180}"
	@echo "make down       detiene y borra contenedores y volúmenes"
	@echo "make e2e-reset  crea la base $(E2E_DB) con el módulo instalado (borra la anterior)"
	@echo "make e2e-setup  configura empresa, timbrado, diarios y conector (SIFENDE_E2E_API_KEY)"
	@echo "make e2e-cases  corre los casos contra la API local (scripts/e2e_cases.py)"
	@echo "make e2e-run    levanta Odoo sobre la base $(E2E_DB) en http://localhost:$${SIFENDE_ODOO_PORT:-8180}"

fetch-oca:
	./scripts/fetch-oca.sh

build:
	docker compose build

up:
	docker compose up -d --wait db

test: fetch-oca build up
	-docker compose exec -T db dropdb -U odoo --if-exists $(DB)
	$(ODOO) $(ADDONS) -d $(DB) -i l10n_py_edi_sifende --test-enable --test-tags $(TEST_TAGS) --stop-after-init --without-demo=true

run: fetch-oca build
	docker compose up odoo

shell:
	docker compose run --rm --no-deps odoo odoo shell $(ADDONS) -d $(DB)

down:
	docker compose down -v

e2e-reset: fetch-oca build up
	-docker compose exec -T db dropdb -U odoo --if-exists $(E2E_DB)
	$(ODOO) $(ADDONS) -d $(E2E_DB) -i l10n_py_edi_sifende,l10n_py --load-language=es_PY --stop-after-init --without-demo=true

e2e-setup:
	$(E2E_SHELL) < scripts/e2e_setup.py

e2e-cases:
	$(E2E_SHELL) < scripts/e2e_cases.py

e2e-run: fetch-oca build
	SIFENDE_ODOO_DB_FILTER='^$(E2E_DB)$$' docker compose up -d odoo
