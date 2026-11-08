import requests

from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import API_KEY, CDC, FakeResponse, SifendeCommon, problem

DOCUMENT_URL = r"^/documento-electronico$"
STALE_REQUEST = '{"stale": true}'


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestRetrySameRequest(SifendeCommon):
    def uncertain(self, move):
        self.route_emission(problem("internal-error", 503))
        move.action_send_edi()
        self.assertEqual(move.l10n_py_edi_status, "error")
        self.assertTrue(move.l10n_py_sifende_reintento_misma_clave)
        return self.fake.calls_to("POST", DOCUMENT_URL)[-1]

    def edit_sources(self):
        self.partner.write(
            {
                "name": "Otro Cliente S.A.",
                "email": "otro@cliente.com.py",
                "street": "Calle Palma 1",
                "ref": "CLI-002",
            }
        )
        self.product.write({"default_code": "OTRO-COD", "l10n_py_ncm_code": "48025620"})
        self.uom_kg.l10n_py_sifende_unidad_medida = 99

    def test_el_reintento_envia_el_mismo_json_aunque_cambien_los_datos_maestros(self):
        move = self.make_move("fe")
        first = self.uncertain(move)
        self.edit_sources()
        self.route_emission(self.emission_response())
        move.action_retry_edi()
        retry = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
        self.assertIsNot(retry, first)
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertEqual(
            retry.headers["Idempotency-Key"], first.headers["Idempotency-Key"]
        )
        self.assertEqual(retry.json, first.json)

    def test_el_reintento_no_se_bloquea_por_datos_maestros_ya_invalidos(self):
        move = self.make_move("fe")
        first = self.uncertain(move)
        self.partner.street = False
        self.route_emission(self.emission_response())
        move.action_retry_edi()
        retry = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertEqual(retry.json, first.json)

    def test_varios_reintentos_inciertos_conservan_la_solicitud_original(self):
        move = self.make_move("fe")
        first = self.uncertain(move)
        self.edit_sources()
        self.route_emission(problem("internal-error", 503))
        move.action_retry_edi()
        self.assertTrue(move.l10n_py_sifende_reintento_misma_clave)
        self.route_emission(self.emission_response())
        move.action_retry_edi()
        calls = self.fake.calls_to("POST", DOCUMENT_URL)
        self.assertEqual({repr(c.json) for c in calls}, {repr(first.json)})
        self.assertEqual(len({c.headers["Idempotency-Key"] for c in calls}), 1)

    def test_la_solicitud_guardada_se_descarta_al_confirmarse_el_envio(self):
        move = self.make_move("fe")
        self.uncertain(move)
        self.assertTrue(move.l10n_py_sifende_request)
        self.route_emission(self.emission_response())
        move.action_retry_edi()
        self.assertFalse(move.l10n_py_sifende_request)

    def test_un_rechazo_definitivo_descarta_la_solicitud_y_la_correccion_la_reconstruye(
        self,
    ):
        move = self.make_move("fe")
        first = self.uncertain(move)
        self.route_emission(problem("validation-error", 400, "Dato inválido"))
        move.action_retry_edi()
        self.assertEqual(move.l10n_py_edi_status, "rejected")
        self.assertFalse(move.l10n_py_sifende_request)
        self.edit_sources()
        self.route_emission(self.emission_response())
        move.action_retry_edi()
        corrected = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
        self.assertNotEqual(corrected.json, first.json)
        self.assertNotEqual(
            corrected.headers["Idempotency-Key"], first.headers["Idempotency-Key"]
        )

    def test_la_solicitud_guardada_no_se_copia_con_el_documento(self):
        move = self.make_move("fe")
        self.uncertain(move)
        self.assertFalse(move.copy().l10n_py_sifende_request)

    # === Solicitud guardada: acceso ===

    def make_biller(self):
        return self.env["res.users"].create(
            {
                "name": "Facturador",
                "login": "facturador-reintento",
                "company_id": self.company.id,
                "company_ids": [(6, 0, self.company.ids)],
                "groups_id": [
                    (
                        6,
                        0,
                        [
                            self.env.ref("base.group_user").id,
                            self.env.ref("account.group_account_invoice").id,
                        ],
                    )
                ],
            }
        )

    def test_el_facturador_envia_y_reintenta_sin_poder_leer_la_solicitud_guardada(self):
        biller = self.make_biller()
        move = self.make_move("fe")
        as_biller = move.with_user(biller)
        self.route_emission(problem("internal-error", 503))
        as_biller.action_send_edi()
        first = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
        self.assertTrue(move.l10n_py_sifende_request)
        with self.assertRaises(AccessError):
            as_biller.mapped("l10n_py_sifende_request")
        with self.assertRaises(AccessError):
            as_biller.read(["l10n_py_sifende_request"])
        self.assertNotIn("l10n_py_sifende_request", as_biller.read()[0])
        self.edit_sources()
        self.route_emission(self.emission_response())
        as_biller.action_retry_edi()
        retry = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertEqual(retry.json, first.json)
        self.assertFalse(move.l10n_py_sifende_request)

    # === Invariante: sin reintento pendiente no hay solicitud guardada ===

    def stale_request(self, move):
        move.sudo().l10n_py_sifende_request = STALE_REQUEST

    def test_renumerar_descarta_la_solicitud_guardada(self):
        move = self.make_move("fe")
        self.route_emission(problem("document-number-in-use", 409, "Número en uso"))
        move.action_send_edi()
        self.stale_request(move)
        move.action_l10n_py_sifende_renumber()
        self.assertFalse(move.l10n_py_sifende_reintento_misma_clave)
        self.assertFalse(move.l10n_py_sifende_request)

    def sent_move(self):
        move = self.make_move("fe")
        self.route_emission(self.emission_response())
        move.action_send_edi()
        self.assertEqual(move.l10n_py_edi_status, "processing")
        return move

    def poll(self, estado, **extra):
        self.fake.add(
            "GET",
            rf"/documento-electronico/status/{CDC}$",
            self.status_response(estado, **extra),
        )
        self.env["account.move"]._cron_sifende_check_status()

    def test_el_rechazo_de_sifen_descarta_la_solicitud_guardada(self):
        move = self.sent_move()
        self.stale_request(move)
        self.poll("RECHAZADO", mensajeRechazo="1001 - CDC duplicado")
        self.assertEqual(move.l10n_py_edi_status, "rejected")
        self.assertFalse(move.l10n_py_sifende_request)

    def test_el_error_de_sifen_descarta_la_solicitud_guardada(self):
        move = self.sent_move()
        self.stale_request(move)
        self.poll("ERROR")
        self.assertEqual(move.l10n_py_edi_status, "error")
        self.assertFalse(move.l10n_py_sifende_request)

    def test_la_aprobacion_de_sifen_descarta_la_solicitud_guardada(self):
        move = self.sent_move()
        self.stale_request(move)
        for suffix, content, kind in (
            ("xml", b"<rDE/>", "xml"),
            ("kude", b"%PDF", "pdf"),
        ):
            self.fake.add(
                "GET",
                rf"/documento-electronico/{CDC}/{suffix}$",
                FakeResponse(
                    200,
                    content=content,
                    headers={"Content-Type": f"application/{kind}"},
                ),
            )
        self.poll("APROBADO")
        self.assertEqual(move.l10n_py_edi_status, "accepted")
        self.assertFalse(move.l10n_py_sifende_request)

    def test_un_envio_con_fallo_definitivo_descarta_la_solicitud_guardada(self):
        move = self.make_move("fe")
        self.uncertain(move)
        self.route_emission(problem("validation-error", 400, "Dato inválido"))
        move.action_retry_edi()
        self.assertFalse(move.l10n_py_sifende_reintento_misma_clave)
        self.assertFalse(move.l10n_py_sifende_request)

    # === Fallos antes del POST ===

    def test_un_fallo_previo_al_post_no_cierra_el_intento_incierto(self):
        move = self.make_move("fe")
        first = self.uncertain(move)
        posts = len(self.fake.calls_to("POST", DOCUMENT_URL))
        stored = move.l10n_py_sifende_request
        self.connector.sifende_api_key = False
        self.edit_sources()
        move.action_retry_edi()
        self.assertEqual(move.l10n_py_edi_status, "error")
        self.assertIn("API key", move.l10n_py_edi_message)
        self.assertTrue(move.l10n_py_sifende_reintento_misma_clave)
        self.assertEqual(move.l10n_py_sifende_request, stored)
        self.assertEqual(move.l10n_py_sifende_intento, 0)
        self.assertEqual(len(self.fake.calls_to("POST", DOCUMENT_URL)), posts)

        self.connector.sifende_api_key = API_KEY
        self.route_emission(self.emission_response())
        move.action_retry_edi()
        retry = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertEqual(
            retry.headers["Idempotency-Key"], first.headers["Idempotency-Key"]
        )
        self.assertEqual(retry.json, first.json)

    def test_un_fallo_previo_al_post_sin_intento_abierto_sigue_cerrandolo(self):
        move = self.make_move("fe")
        self.connector.sifende_api_key = False
        move.action_send_edi()
        self.assertEqual(move.l10n_py_edi_status, "error")
        self.assertFalse(move.l10n_py_sifende_reintento_misma_clave)
        self.assertFalse(move.l10n_py_sifende_request)

    # === Errores de transporte y idempotencia en curso ===

    def test_un_error_de_transporte_guarda_la_solicitud_y_el_reintento_la_reenvia(self):
        for error in (requests.Timeout(), requests.ConnectionError()):
            with self.subTest(error=type(error).__name__):
                move = self.make_move("fe")
                self.route_emission(error)
                move.action_send_edi()
                first = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
                self.assertEqual(move.l10n_py_edi_status, "error")
                self.assertTrue(move.l10n_py_sifende_reintento_misma_clave)
                self.assertTrue(move.l10n_py_sifende_request)
                self.edit_sources()
                self.route_emission(self.emission_response())
                move.action_retry_edi()
                retry = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
                self.assertEqual(move.l10n_py_edi_status, "processing")
                self.assertEqual(retry.json, first.json)
                self.assertEqual(
                    retry.headers["Idempotency-Key"], first.headers["Idempotency-Key"]
                )
                self.assertFalse(move.l10n_py_sifende_request)

    def test_idempotencia_en_curso_guarda_la_solicitud_y_el_reintento_la_reenvia(self):
        move = self.make_move("fe")
        self.route_emission(
            FakeResponse(
                409,
                {
                    "type": "https://sifende.com.py/docs/solucion-problemas/"
                    "idempotency-in-progress"
                },
                headers={"Retry-After": "3"},
            )
        )
        move.action_send_edi()
        first = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
        self.assertEqual(move.l10n_py_edi_status, "error")
        self.assertTrue(move.l10n_py_sifende_reintento_misma_clave)
        self.assertTrue(move.l10n_py_sifende_request)
        self.edit_sources()
        self.route_emission(self.emission_response())
        move.action_retry_edi()
        retry = self.fake.calls_to("POST", DOCUMENT_URL)[-1]
        self.assertEqual(move.l10n_py_edi_status, "processing")
        self.assertEqual(retry.json, first.json)
        self.assertEqual(
            retry.headers["Idempotency-Key"], first.headers["Idempotency-Key"]
        )
