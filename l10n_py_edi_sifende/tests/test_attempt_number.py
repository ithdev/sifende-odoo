from odoo.tests import tagged

from .common import SifendeCommon, problem

DOCUMENT_URL = r"^/documento-electronico$"


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestAttemptNumber(SifendeCommon):
    """Cada envío tras un intento cerrado usa una clave de idempotencia nueva.

    Todos los caminos que llegan a ``action_send_edi`` (botón «Enviar a EDI»,
    asistente de OCA, repetir operación del registro, cola de contingencia,
    borrador y publicar) cumplen «misma clave, misma solicitud» solo mientras
    el intento anterior sea incierto.
    """

    def keys(self):
        return [
            call.headers["Idempotency-Key"]
            for call in self.fake.calls_to("POST", DOCUMENT_URL)
        ]

    def reject(self, move):
        self.route_emission(problem("validation-error", 400, "Dato inválido"))
        move.action_send_edi()
        self.assertEqual(move.l10n_py_edi_status, "rejected")
        self.assertFalse(move.l10n_py_sifende_reintento_misma_clave)

    def fix_data(self):
        self.partner.street = "Calle Palma 1"

    def test_el_boton_enviar_tras_un_rechazo_usa_una_clave_nueva(self):
        move = self.make_move("fe")
        self.reject(move)
        self.fix_data()
        self.route_emission(self.emission_response())
        move.action_send_edi()
        keys = self.keys()
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(move.l10n_py_sifende_intento, 1)

    def test_el_boton_enviar_tras_un_error_definitivo_usa_una_clave_nueva(self):
        move = self.make_move("fe")
        self.route_emission(problem("timbrado-no-vigente", 422, "Detalle"))
        move.action_send_edi()
        self.assertEqual(move.l10n_py_edi_status, "error")
        self.assertFalse(move.l10n_py_sifende_reintento_misma_clave)
        self.route_emission(self.emission_response())
        move.action_send_edi()
        keys = self.keys()
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertNotEqual(keys[0], keys[1])

    def test_el_asistente_de_envio_de_oca_tras_un_rechazo_usa_una_clave_nueva(self):
        move = self.make_move("fe")
        self.reject(move)
        self.route_emission(self.emission_response())
        wizard = self.env["account.move.send.edi.wizard"].create(
            {"invoice_id": move.id}
        )
        wizard.action_send()
        keys = self.keys()
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertNotEqual(keys[0], keys[1])

    def test_repetir_la_operacion_del_registro_tras_un_rechazo_usa_una_clave_nueva(
        self,
    ):
        move = self.make_move("fe")
        self.reject(move)
        log = self.env["l10n_py.edi.log"].create(
            {
                "operation_type": "send",
                "document_id": move.id,
                "provider": "sifende",
            }
        )
        self.route_emission(self.emission_response())
        log.action_retry_operation()
        keys = self.keys()
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertNotEqual(keys[0], keys[1])

    def test_rechazado_a_borrador_y_publicar_de_nuevo_usa_una_clave_nueva(self):
        move = self.make_move("fe")
        self.reject(move)
        move.button_draft()
        move.action_post()
        self.assertEqual(move.l10n_py_edi_status, "to_send")
        self.route_emission(self.emission_response())
        move.action_send_edi()
        keys = self.keys()
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertNotEqual(keys[0], keys[1])

    def test_varios_envios_cerrados_suben_el_intento_de_uno_en_uno(self):
        move = self.make_move("fe")
        self.reject(move)
        self.route_emission(problem("validation-error", 400, "Otro dato inválido"))
        move.action_send_edi()
        self.route_emission(self.emission_response())
        move.action_send_edi()
        keys = self.keys()
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual([key.rsplit(":", 1)[1] for key in keys], ["0", "1", "2"])

    def test_el_boton_enviar_con_el_intento_incierto_reenvia_la_misma_solicitud(self):
        move = self.make_move("fe")
        self.route_emission(problem("internal-error", 503))
        move.action_send_edi()
        self.assertTrue(move.l10n_py_sifende_reintento_misma_clave)
        self.fix_data()
        self.route_emission(self.emission_response())
        move.action_send_edi()
        calls = self.fake.calls_to("POST", DOCUMENT_URL)
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertEqual(len({c.headers["Idempotency-Key"] for c in calls}), 1)
        self.assertEqual({repr(c.json) for c in calls}, {repr(calls[0].json)})
        self.assertEqual(move.l10n_py_sifende_intento, 0)

    def test_reintentar_sube_el_intento_una_sola_vez(self):
        move = self.make_move("fe")
        self.reject(move)
        self.route_emission(self.emission_response())
        move.action_retry_edi()
        self.assertEqual(move.l10n_py_sifende_intento, 1)
        self.assertEqual(len(set(self.keys())), 2)

    def test_el_primer_envio_conserva_el_intento_cero(self):
        move = self.make_move("fe")
        self.route_emission(self.emission_response())
        move.action_send_edi()
        self.assertEqual(move.l10n_py_sifende_intento, 0)
        self.assertTrue(self.keys()[0].endswith(f":{move.id}:0"))
