from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import CDC, FakeResponse, SifendeCommon, problem

INUTILIZE_URL = r"/documento-electronico/inutilizar$"
CANCEL_URL = rf"/documento-electronico/{CDC}/cancelar$"


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestRetryEvents(SifendeCommon):
    def inutilization(self, **vals):
        self.auths["fe"].series = "AB"
        return self.env["l10n_py.number.inutilization"].create(
            {
                "authorization_id": self.auths["fe"].id,
                "number_from": 5,
                "number_to": 7,
                "motive": "Números saltados por error de sistema",
                **vals,
            }
        )

    def send_uncertain(self, inutilization):
        self.fake.add("POST", INUTILIZE_URL, problem("internal-error", 503))
        self.fails(inutilization.action_send)
        # el rollback del error que ve el usuario deshace el estado, no la clave
        inutilization.state = "draft"
        return self.fake.calls_to("POST", INUTILIZE_URL)[-1]

    def fails(self, method):
        # sin assertRaises: su savepoint desharía la clave confirmada aparte
        try:
            method()
        except UserError as e:
            return str(e)
        self.fail("Se esperaba un UserError")

    def accepted_move(self):
        move = self.make_move("fe", invoice_date=fields.Date.today())
        move.write({"l10n_py_edi_status": "accepted", "l10n_py_cdc": CDC})
        return move

    def cancel_uncertain(self, move, motive):
        self.fake.add("POST", CANCEL_URL, problem("internal-error", 503))
        wizard = self.env["l10n_py.edi.cancel.wizard"].create(
            {"invoice_id": move.id, "motive": motive}
        )
        self.fails(wizard.action_cancel)
        self.assertEqual(move.l10n_py_edi_status, "accepted")
        return self.fake.calls_to("POST", CANCEL_URL)[-1]

    # === Inutilización ===

    def test_la_inutilizacion_reintenta_el_mismo_cuerpo_aunque_cambie_el_timbrado(self):
        inutilization = self.inutilization()
        first = self.send_uncertain(inutilization)
        self.assertEqual(first.json["serie"], "AB")
        self.auths["fe"].write({"series": "CD", "name": "11111111"})
        self.fake.add(
            "POST", INUTILIZE_URL, FakeResponse(200, {"estadoEvento": "APROBADO"})
        )
        inutilization.action_send()
        retry = self.fake.calls_to("POST", INUTILIZE_URL)[-1]
        self.assertIsNot(retry, first)
        self.assertEqual(inutilization.state, "accepted")
        self.assertEqual(
            retry.headers["Idempotency-Key"], first.headers["Idempotency-Key"]
        )
        self.assertEqual(retry.json, first.json)

    def test_la_inutilizacion_confirmada_descarta_clave_y_solicitud(self):
        inutilization = self.inutilization()
        self.send_uncertain(inutilization)
        self.assertTrue(inutilization.l10n_py_sifende_event_request)
        self.fake.add(
            "POST", INUTILIZE_URL, FakeResponse(200, {"estadoEvento": "APROBADO"})
        )
        inutilization.action_send()
        self.assertFalse(inutilization.l10n_py_sifende_event_key)
        self.assertFalse(inutilization.l10n_py_sifende_event_request)

    def test_el_rechazo_definitivo_de_una_inutilizacion_descarta_la_solicitud(self):
        inutilization = self.inutilization()
        self.send_uncertain(inutilization)
        self.fake.add(
            "POST",
            INUTILIZE_URL,
            problem("evento-inutilizacion-error", 400, "Rango en uso"),
        )
        self.assertIn("Rango en uso", self.fails(inutilization.action_send))
        self.assertFalse(inutilization.l10n_py_sifende_event_key)
        self.assertFalse(inutilization.l10n_py_sifende_event_request)

    def test_la_solicitud_de_una_inutilizacion_no_se_copia(self):
        inutilization = self.inutilization()
        self.send_uncertain(inutilization)
        copy = inutilization.copy()
        self.assertFalse(copy.l10n_py_sifende_event_key)
        self.assertFalse(copy.l10n_py_sifende_event_request)

    # === Cancelación ===

    def test_la_cancelacion_retoma_el_motivo_original_con_la_misma_clave(self):
        move = self.accepted_move()
        first = self.cancel_uncertain(move, "Error en el monto facturado")
        self.fake.add(
            "POST", CANCEL_URL, FakeResponse(200, {"estadoEvento": "APROBADO"})
        )
        wizard = self.env["l10n_py.edi.cancel.wizard"].create(
            {"invoice_id": move.id, "motive": "Otro motivo distinto del original"}
        )
        wizard.action_cancel()
        retry = self.fake.calls_to("POST", CANCEL_URL)[-1]
        self.assertIsNot(retry, first)
        self.assertEqual(move.l10n_py_edi_status, "cancelled")
        self.assertEqual(
            retry.headers["Idempotency-Key"], first.headers["Idempotency-Key"]
        )
        self.assertEqual(retry.json, {"motivo": "Error en el monto facturado"})

    def test_el_asistente_muestra_el_motivo_original_de_una_cancelacion_pendiente(
        self,
    ):
        move = self.accepted_move()
        self.cancel_uncertain(move, "Error en el monto facturado")
        wizard = (
            self.env["l10n_py.edi.cancel.wizard"]
            .with_context(active_id=move.id)
            .new({})
        )
        self.assertEqual(wizard.motive, "Error en el monto facturado")
        self.assertTrue(wizard.l10n_py_sifende_pending)

    def test_default_get_no_devuelve_el_motivo_si_no_se_pidio(self):
        move = self.accepted_move()
        self.cancel_uncertain(move, "Error en el monto facturado")
        wizard = self.env["l10n_py.edi.cancel.wizard"].with_context(active_id=move.id)
        self.assertNotIn("motive", wizard.default_get(["invoice_id"]))
        self.assertEqual(
            wizard.default_get(["motive"])["motive"], "Error en el monto facturado"
        )

    def test_el_asistente_no_bloquea_el_motivo_sin_cancelacion_pendiente(self):
        move = self.accepted_move()
        wizard = (
            self.env["l10n_py.edi.cancel.wizard"]
            .with_context(active_id=move.id)
            .new({})
        )
        self.assertFalse(wizard.motive)
        self.assertFalse(wizard.l10n_py_sifende_pending)

    def test_un_rechazo_definitivo_de_la_cancelacion_libera_el_motivo(self):
        move = self.accepted_move()
        self.cancel_uncertain(move, "Error en el monto facturado")
        self.fake.add(
            "POST",
            CANCEL_URL,
            problem("evento-cancelacion-error", 400, "Plazo vencido"),
        )
        self.assertIn("Plazo vencido", self.fails(move.action_cancel_edi))
        self.assertFalse(move.l10n_py_sifende_event_key)
        self.assertFalse(move.l10n_py_sifende_event_request)
        self.fake.add(
            "POST", CANCEL_URL, FakeResponse(200, {"estadoEvento": "APROBADO"})
        )
        wizard = self.env["l10n_py.edi.cancel.wizard"].create(
            {"invoice_id": move.id, "motive": "Motivo nuevo tras el rechazo"}
        )
        wizard.action_cancel()
        call = self.fake.calls_to("POST", CANCEL_URL)[-1]
        self.assertEqual(call.json, {"motivo": "Motivo nuevo tras el rechazo"})
