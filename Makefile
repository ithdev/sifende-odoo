DB ?= sifende_odoo
TEST_TAGS ?= /l10n_py_edi_sifende
ODOO = docker compose run --rm --no-deps -T odoo odoo
ADDONS = --addons-path=/mnt/oca/l10n-paraguay,/mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons

.PHONY: help fetch-oca build up down test shell run

help:
	@echo "make fetch-oca  clona OCA/l10n-paraguay (pineado) en vendor/"
	@echo "make build      construye la imagen de Odoo con las dependencias"
	@echo "make test       instala el módulo en una base nueva y corre sus tests"
	@echo "make run        levanta Odoo en http://localhost:$${SIFENDE_ODOO_PORT:-8179}"
	@echo "make down       detiene y borra contenedores y volúmenes"

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
