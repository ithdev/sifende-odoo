# ruff: noqa: F821
# pylint: disable=print-used
"""Casos de punta a punta del conector contra la API de Sifende en SANDBOX.

    odoo shell -d sifende_e2e < scripts/e2e_cases.py

Requiere scripts/e2e_setup.py ya ejecutado. SIFENDE_E2E_CASES elige casos ("1,2,3"),
SIFENDE_E2E_DEV_API_KEY (clave sk_test_ del mismo contribuyente) habilita el caso 1c.
"""

import base64
import json
import os
import re
import time
import uuid
from datetime import date
from unittest.mock import patch

import requests

CASES = {
    int(c)
    for c in (os.environ.get("SIFENDE_E2E_CASES") or "1,2,3,4,5,6,7,8").split(",")
}

company = env["res.company"].search([("name", "=", "Empresa E2E")], limit=1)
env = env(context=dict(env.context, allowed_company_ids=[company.id]))
env = env(user=env.ref("base.user_admin").id)
Move = env["account.move"]
connector = env["l10n_py.edi.connector"].search(
    [("company_id", "=", company.id)], limit=1
)
product = {p.default_code: p for p in env["product.product"].search([])}
partner = {
    "b2b": env["res.partner"].search(
        [("name", "=", "Comercial San Roque S.A.")], limit=1
    ),
    "b2c": env["res.partner"].search([("name", "=", "Ana González")], limit=1),
}
journal = {
    "fe": env["account.journal"].search(
        [("company_id", "=", company.id), ("code", "=", "FE")]
    ),
    "nc": env["account.journal"].search(
        [("company_id", "=", company.id), ("code", "=", "NCE")]
    ),
}
tax = {
    rate: env["account.chart.template"].with_company(company).ref(xmlid)
    for rate, xmlid in (
        ("10", "py_tax_vat_10_ventas"),
        ("5", "py_tax_vat_5_ventas"),
        ("0", "py_tax_vat_exento_ventas"),
    )
}


def out(label, **values):
    print(f"E2E[{label}]", json.dumps(values, ensure_ascii=False, default=str))


def line(code, qty, price, rate):
    return (
        0,
        0,
        {
            "product_id": product[code].id,
            "name": product[code].name,
            "quantity": qty,
            "price_unit": price,
            "tax_ids": [(6, 0, tax[rate].ids)],
        },
    )


def invoice(kind, who, lines, **vals):
    move = Move.create(
        {
            "move_type": "out_refund" if kind == "nc" else "out_invoice",
            "partner_id": partner[who].id,
            "journal_id": journal[kind].id,
            "invoice_date": date.today(),
            "l10n_latam_document_type_id": journal[
                kind
            ].l10n_py_authorization_id.l10n_latam_document_type_id.id,
            "l10n_py_authorization_id": journal[kind].l10n_py_authorization_id.id,
            "invoice_line_ids": lines,
            **vals,
        }
    )
    move.action_post()
    return move


def snapshot(label, move):
    move.invalidate_recordset()
    cdc = move.l10n_py_cdc or ""
    out(
        label,
        move=move.id,
        numero=move.l10n_py_full_invoice_number,
        total=move.amount_total,
        estado=move.l10n_py_edi_status,
        cdc=cdc,
        cdc_numero=f"{cdc[11:14]}-{cdc[14:17]}-{cdc[17:24]}"
        if len(cdc) == 44
        else None,
        mensaje=move.l10n_py_edi_message,
        xml=bool(move.l10n_py_edi_xml),
        kude=bool(move.l10n_py_kude_pdf),
    )


def send(label, move):
    move.action_send_edi()
    env.cr.commit()
    snapshot(label, move)


def cron():
    Move._cron_sifende_check_status()
    env.cr.commit()


def api_get(path):
    key = connector.sudo().sifende_api_key
    return requests.get(
        f"{connector._sifende_base_url()}/api/v1{path}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )


def notification(action):
    params = action["params"]
    return {
        "type": params["type"],
        "title": params["title"],
        "message": params["message"],
    }


def wait_files(move, rounds=12):
    for _ in range(rounds):
        cron()
        move.invalidate_recordset()
        if move.l10n_py_edi_xml and move.l10n_py_kude_pdf:
            break
        time.sleep(5)


def attachments(move):
    found = {}
    for att in env["ir.attachment"].search(
        [
            ("res_model", "=", "account.move"),
            ("res_id", "=", move.id),
            ("res_field", "!=", False),
        ]
    ):
        found[att.res_field] = att.file_size
    return found


def check_files(label, move):
    xml = base64.b64decode(move.l10n_py_edi_xml or b"")
    pdf = base64.b64decode(move.l10n_py_kude_pdf or b"")
    number = re.search(rb"<dNumDoc>(\d+)</dNumDoc>", xml)
    out(
        label,
        xml_bytes=len(xml),
        xml_dNumDoc=number.group(1).decode() if number else None,
        xml_cdc=bool(move.l10n_py_cdc and move.l10n_py_cdc.encode() in xml),
        kude_bytes=len(pdf),
        kude_es_pdf=pdf[:5] == b"%PDF-",
        adjuntos=attachments(move),
    )


def sifende_status(cdc):
    response = api_get(f"/documento-electronico/status/{cdc}")
    body = response.json() if response.content else {}
    return {k: body.get(k) for k in ("estado", "numeroDocumento", "ambiente")}


def associate(move, other):
    env["l10n_py.associated.document"].create(
        {"move_id": move.id, "association_type": "1", "cdc": other.l10n_py_cdc}
    )


def api_emit(payload, key=None):
    headers = {
        "Authorization": f"Bearer {connector.sudo().sifende_api_key}",
        "Idempotency-Key": key or str(uuid.uuid4()),
    }
    return requests.post(
        f"{connector._sifende_base_url()}/api/v1/documento-electronico",
        json=payload,
        headers=headers,
        timeout=60,
    )


def last_payload(move):
    log = env["l10n_py.edi.log"].search(
        [("operation_type", "=", "send"), ("document_id", "=", move.id)],
        order="id desc",
        limit=1,
    )
    return json.loads(log.request_data)


moves = {}

# === 1. Probar conexión ===
if 1 in CASES:
    out("1a test_connection", **notification(connector.test_connection()))
    other_point = journal["fe"].l10n_py_authorization_id.copy(
        {"establishment": "002", "expedition_point": "009", "name": "87654321"}
    )
    out("1b punto y timbrado desconocidos", **notification(connector.test_connection()))
    dev_key = os.environ.get("SIFENDE_E2E_DEV_API_KEY")
    if dev_key:
        sandbox_key = connector.sudo().sifende_api_key
        connector.sudo().sifende_api_key = dev_key
        out(
            "1c clave DEV, timbrado distinto",
            **notification(connector.test_connection()),
        )
        connector.sudo().sifende_api_key = sandbox_key
    other_point.unlink()
    env.cr.commit()

# === 2. Factura contado B2B ===
if 2 in CASES:
    moves["fe1"] = invoice(
        "fe",
        "b2b",
        [
            line("PAP-A4", 3, 11000.0, "10"),
            line("YERBA", 1, 52500.0, "5"),
            line("CAP-01", 1, 200000.0, "0"),
        ],
        l10n_py_security_code="123456789",
    )
    send("2 enviada", moves["fe1"])
    wait_files(moves["fe1"])
    snapshot("2 tras cron", moves["fe1"])
    check_files("2 archivos", moves["fe1"])
    out("2 estado en sifende", **sifende_status(moves["fe1"].l10n_py_cdc))

# === 3. Crédito en cuotas y B2C ===
if 3 in CASES:
    term = env["account.payment.term"].create(
        {
            "name": "3 cuotas 30/60/90",
            "line_ids": [
                (
                    0,
                    0,
                    {
                        "value": "percent",
                        "value_amount": pct,
                        "delay_type": "days_after",
                        "nb_days": days,
                    },
                )
                for days, pct in ((30, 33.33), (60, 33.33), (90, 33.34))
            ],
        }
    )
    moves["fe2"] = invoice(
        "fe",
        "b2b",
        [line("PAP-A4", 10, 11000.0, "10"), line("YERBA", 2, 52500.0, "5")],
        invoice_payment_term_id=term.id,
        l10n_py_security_code="223456789",
    )
    send("3a crédito en cuotas", moves["fe2"])
    moves["fe3"] = invoice(
        "fe",
        "b2c",
        [line("PAP-A4", 2, 11000.0, "10")],
        l10n_py_security_code="323456789",
    )
    send("3b B2C no contribuyente", moves["fe3"])
    for key in ("fe2", "fe3"):
        wait_files(moves[key])
        snapshot(f"3 {key} tras cron", moves[key])
        check_files(f"3 {key} archivos", moves[key])
    payload = last_payload(moves["fe2"])
    out(
        "3a cuotas enviadas",
        condicion=payload.get("condicionOperacion"),
        credito=payload.get("credito"),
    )

# === 4. Nota de crédito sobre la factura 2 ===
if 4 in CASES:
    base = (
        moves.get("fe2") or Move.search([("l10n_py_cdc", "!=", False)], order="id")[1]
    )
    reversal = (
        env["account.move.reversal"]
        .with_context(active_model="account.move", active_ids=base.ids)
        .create({"journal_id": journal["nc"].id, "reason": "Devolución parcial"})
    )
    nc = Move.browse(reversal.refund_moves()["res_id"])
    nc.write(
        {
            "l10n_py_authorization_id": journal["nc"].l10n_py_authorization_id.id,
            "l10n_py_security_code": "423456789",
        }
    )
    associate(nc, base)
    nc.action_post()
    out(
        "4 nota de crédito",
        motivo=nc.l10n_py_sifende_motivo_emision,
        tipo=nc.l10n_latam_document_type_id.name,
    )
    send("4 NC enviada", nc)
    wait_files(nc)
    snapshot("4 NC tras cron", nc)
    check_files("4 NC archivos", nc)
    moves["nc"] = nc

# === 5. Número en uso ===
if 5 in CASES:
    used = moves.get("fe1") or Move.search(
        [("l10n_py_cdc", "!=", False)], order="id", limit=1
    )
    other_auth = journal["fe"].l10n_py_authorization_id.copy({"name": "87654321"})
    other_journal = journal["fe"].copy(
        {"name": "Facturas (timbrado nuevo)", "code": "FE2"}
    )
    other_journal.l10n_py_authorization_id = other_auth
    journal["fe2"] = other_journal
    clash = invoice(
        "fe2",
        "b2b",
        [line("PAP-A4", 1, 11000.0, "10")],
        l10n_py_authorization_id=other_auth.id,
        l10n_py_security_code="523456789",
        l10n_py_invoice_number=used.l10n_py_invoice_number,
    )
    send("5 número en uso", clash)
    out("5 reintento sin corregir", conserva_error=clash.l10n_py_edi_status == "error")
    moves["clash"] = clash

# === 6. Reintento idempotente tras un timeout ===
if 6 in CASES:
    retry = invoice(
        "fe",
        "b2b",
        [line("PAP-A4", 4, 11000.0, "10")],
        l10n_py_security_code="623456789",
    )
    real_request = requests.request
    attempts = []

    def lost_response(method, url, **kwargs):
        response = real_request(method, url, **kwargs)
        if method == "POST" and url.endswith("/documento-electronico"):
            attempts.append(kwargs["headers"]["Idempotency-Key"])
            raise requests.Timeout("respuesta perdida")
        return response

    with patch("requests.request", lost_response), patch.object(time, "sleep"):
        retry.action_send_edi()
    snapshot("6a tras timeout", retry)
    out(
        "6a intentos",
        envios=len(attempts),
        misma_clave=len(set(attempts)) == 1,
        reintento_misma_clave=retry.l10n_py_sifende_reintento_misma_clave,
    )
    env.cr.commit()
    retry.action_retry_edi()
    env.cr.commit()
    snapshot("6b reintento", retry)
    out(
        "6b clave",
        clave_del_reintento=retry._l10n_py_sifende_idempotency_key() in attempts,
    )
    wait_files(retry)
    snapshot("6c tras cron", retry)
    moves["retry"] = retry

# === 7. Cancelación de la factura 2 ===
if 7 in CASES:
    target = (
        moves.get("fe2") or Move.search([("l10n_py_cdc", "!=", False)], order="id")[1]
    )
    wizard = env["l10n_py.edi.cancel.wizard"].create(
        {"invoice_id": target.id, "motive": "Error en la cantidad facturada"}
    )
    try:
        wizard.action_cancel()
    except Exception as e:
        out("7 cancelación falló", error=str(e))
    env.cr.commit()
    snapshot("7 tras cancelar", target)
    out("7 estado en sifende", **sifende_status(target.l10n_py_cdc))

# === 8. Numeración automática del panel ===
if 8 in CASES:
    sent = Move.search(
        [("l10n_py_cdc", "!=", False), ("journal_id", "=", journal["fe"].id)]
    )
    highest = max(sent.mapped("l10n_py_invoice_number"))
    base = last_payload(sent.sorted("l10n_py_invoice_number")[-1])
    for field in ("numeroDocumento", "codigoSeguridad", "numeroTimbrado"):
        base.pop(field, None)
    response = api_emit(base)
    body = response.json()
    out(
        "8 emisión por API sin numeroDocumento",
        maximo_odoo=highest,
        http=response.status_code,
        numero=body.get("numeroFormateado"),
        numeroDocumento=body.get("numeroDocumento"),
        cdc=body.get("cdc"),
        detalle=body.get("detail"),
    )
