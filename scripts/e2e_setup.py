# ruff: noqa: F821
# pylint: disable=print-used
"""Prepara una base de Odoo para probar el conector contra una API de Sifende local.

    odoo shell -d sifende_e2e < scripts/e2e_setup.py

Variables: SIFENDE_E2E_API_KEY (obligatoria), SIFENDE_E2E_BASE_URL, SIFENDE_E2E_RUC,
SIFENDE_E2E_STAMP, SIFENDE_E2E_FIRST_NUMBER. Es idempotente.
"""

import os
from datetime import date, timedelta

API_KEY = os.environ["SIFENDE_E2E_API_KEY"]
BASE_URL = os.environ.get("SIFENDE_E2E_BASE_URL", "https://api.sifende.com.py")
RUC = os.environ["SIFENDE_E2E_RUC"]
STAMP = os.environ.get("SIFENDE_E2E_STAMP", "12345678")
FIRST_NUMBER = int(os.environ.get("SIFENDE_E2E_FIRST_NUMBER", "1"))
COMPANY_NAME = "Empresa E2E"

py = env.ref("base.py")
pyg = env.ref("base.PYG")
pyg.active = True
env["res.lang"]._activate_lang("es_PY")

company = env["res.company"].search([("name", "=", COMPANY_NAME)], limit=1)
if not company:
    company = env["res.company"].create(
        {
            "name": COMPANY_NAME,
            "country_id": py.id,
            "currency_id": pyg.id,
            "street": "Av. Mariscal López 1234",
            "city": "Asunción",
        }
    )
company.write({"l10n_py_ruc": RUC, "account_fiscal_country_id": py.id})
admin = env.ref("base.user_admin")
admin.write(
    {"company_ids": [(4, company.id)], "company_id": company.id, "lang": "es_PY"}
)
env = env(context=dict(env.context, allowed_company_ids=[company.id]))
env = env(user=admin.id)
company = env["res.company"].browse(company.id)

if company.chart_template != "py":
    env["account.chart.template"].try_loading("py", company, install_demo=False)

template = env["account.chart.template"].with_company(company)
taxes = {
    "10": template.ref("py_tax_vat_10_ventas"),
    "5": template.ref("py_tax_vat_5_ventas"),
    "0": template.ref("py_tax_vat_exento_ventas"),
}
taxes_all = env["account.tax"].browse([t.id for t in taxes.values()])
taxes_all.write({"price_include_override": "tax_included"})

# === Timbrado y diarios (FE, NC) ===
today = date.today()
journals = {}
for key, xmlid, name, code in (
    ("fe", "l10n_py_account.dc_py_f", "Facturas electrónicas", "FE"),
    ("nc", "l10n_py_account.dc_py_nc", "Notas de crédito electrónicas", "NCE"),
):
    doc_type = env.ref(xmlid)
    auth = env["account.authorization"].search(
        [
            ("company_id", "=", company.id),
            ("name", "=", STAMP),
            ("l10n_latam_document_type_id", "=", doc_type.id),
        ],
        limit=1,
    ) or env["account.authorization"].create(
        {
            "name": STAMP,
            "date_from": today - timedelta(days=30),
            "date_to": today + timedelta(days=335),
            "invoice_number_from": FIRST_NUMBER,
            "invoice_number_to": 9999999,
            "establishment": "001",
            "expedition_point": "001",
            "l10n_latam_document_type_id": doc_type.id,
            "company_id": company.id,
        }
    )
    journal = env["account.journal"].search(
        [("company_id", "=", company.id), ("code", "=", code)], limit=1
    ) or env["account.journal"].create(
        {
            "name": name,
            "type": "sale",
            "code": code,
            "company_id": company.id,
            "l10n_latam_use_documents": True,
            "l10n_py_authorization_id": auth.id,
            "l10n_py_establishment": "001",
            "l10n_py_point": "001",
        }
    )
    journals[key] = journal

# === Partners ===
state = env["res.country.state"].search(
    [("country_id", "=", py.id), ("l10n_py_code", "=", 1)], limit=1
)
city = env["res.city"].search([("state_id", "=", state.id)], limit=1)
partner_vals = {
    "country_id": py.id,
    "state_id": state.id,
    "city_id": city.id,
    "street": "Avenida España 1500",
}
partners = {}
for key, name, vals in (
    (
        "b2b",
        "Comercial San Roque S.A.",
        {
            "is_company": True,
            "l10n_py_ruc": "80012345",
            "l10n_py_taxpayer_type": "1",
            "email": "facturacion@sanroque.com.py",
            "phone": "+595 21 444 1111",
            "ref": "CLI-001",
        },
    ),
    (
        "b2c",
        "Ana González",
        {
            "l10n_py_taxpayer_type": "2",
            "l10n_py_doc_type": "1",
            "l10n_py_doc_number": "4567890",
            "email": "ana@example.com",
        },
    ),
):
    partner = env["res.partner"].search([("name", "=", name)], limit=1)
    if not partner:
        partner = env["res.partner"].create({"name": name, **partner_vals, **vals})
    partners[key] = partner

# === Productos ===
products = {}
for key, name, code, price, ncm, tax in (
    ("iva10", "Resma de papel A4", "PAP-A4", 11000.0, "48025610", "10"),
    ("iva5", "Yerba mate", "YERBA", 52500.0, "09030090", "5"),
    ("exento", "Libro de capacitación", "CAP-01", 200000.0, "49019900", "0"),
):
    product = env["product.product"].search([("default_code", "=", code)], limit=1)
    if not product:
        product = env["product.product"].create(
            {
                "name": name,
                "default_code": code,
                "list_price": price,
                "l10n_py_ncm_code": ncm,
                "taxes_id": [(6, 0, taxes[tax].ids)],
            }
        )
    products[key] = product

# === Conector ===
connector = env["l10n_py.edi.connector"].search(
    [("company_id", "=", company.id), ("provider_type", "=", "sifende")], limit=1
)
vals = {
    "name": "Sifende (E2E local)",
    "provider_type": "sifende",
    "environment": "test",
    "company_id": company.id,
    "sifende_api_key": API_KEY,
    "sifende_base_url": BASE_URL,
}
if connector:
    connector.write(vals)
else:
    connector = env["l10n_py.edi.connector"].create(vals)

env.cr.commit()
print(
    "E2E_SETUP_OK",
    {
        "company": company.id,
        "connector": connector.id,
        "journals": {k: j.id for k, j in journals.items()},
        "partners": {k: p.id for k, p in partners.items()},
        "products": {k: p.id for k, p in products.items()},
    },
)
