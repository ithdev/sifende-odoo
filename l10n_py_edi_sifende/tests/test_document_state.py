from datetime import timedelta
from itertools import product
from unittest.mock import patch

import requests

from odoo import fields
from odoo.tests import BaseCase, tagged

from odoo.addons.l10n_py_edi_sifende.services.document_state import (
    DocumentState,
    StatusUnavailable,
)

from .common import CDC, FakeResponse, SifendeCommon, problem

PDF = b"%PDF-1.4 kude"
XML = b"<rDE/>"
ACCOUNT_MOVE_LOGGER = "odoo.addons.l10n_py_edi_sifende.models.account_move"

# estados de arranque: nombre -> (l10n_py_edi_status, l10n_py_sifende_envio_fallido)
STARTS = {
    "draft": ("draft", False),
    "processing": ("processing", False),
    "accepted": ("accepted", False),
    "failed": ("error", True),
    "error": ("error", False),
    "rejected": ("rejected", False),
    "cancelled": ("cancelled", False),
}
# qué cambia cada estado remoto; lo que no está aquí deja el documento como estaba
TRANSITIONS = {
    ("APROBADO", "processing"): ("accepted", False),
    ("APROBADO", "failed"): ("accepted", False),
    ("APROBADO_OBSERVACION", "processing"): ("accepted", False),
    ("APROBADO_OBSERVACION", "failed"): ("accepted", False),
    ("RECHAZADO", "processing"): ("rejected", False),
    ("RECHAZADO", "failed"): ("rejected", False),
    ("ERROR", "processing"): ("error", True),
    ("PENDIENTE", "failed"): ("processing", False),
    ("EN_LOTE", "failed"): ("processing", False),
    ("ENVIADO", "failed"): ("processing", False),
    ("CANCELADO", "processing"): ("cancelled", False),
    ("CANCELADO", "accepted"): ("cancelled", False),
}
REMOTE_STATES = (
    "APROBADO",
    "APROBADO_OBSERVACION",
    "RECHAZADO",
    "ERROR",
    "PENDIENTE",
    "EN_LOTE",
    "ENVIADO",
    "CANCELADO",
    "DESCONOCIDO",
)
# fallas de la consulta: nombre -> (respuesta del servidor, ¿el log la marca exitosa?)
QUERY_FAILURES = {
    "timeout": (requests.Timeout(), False),
    "503": (problem("internal-error", 503, "no disponible"), False),
    "200_sin_estado": (FakeResponse(200, {"cdc": CDC}), True),
    "200_no_objeto": (FakeResponse(200, ["no", "es", "un", "objeto"]), True),
}


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestDocumentState(SifendeCommon):
    def tracked(self, start="processing", **vals):
        move = self.make_move(
            "fe", invoice_date=fields.Date.context_today(self.env.user)
        )
        status, failed = STARTS[start]
        move.write(
            {
                "l10n_py_edi_status": status,
                "l10n_py_sifende_envio_fallido": failed,
                "l10n_py_cdc": CDC,
                **vals,
            }
        )
        return move

    def release(self, move):
        # saca el documento de la cola del cron para el siguiente caso del bucle
        move.write(
            {
                "l10n_py_cdc": False,
                "l10n_py_edi_status": "draft",
                "l10n_py_sifende_envio_fallido": False,
                "l10n_py_sifende_archivos_pendientes": False,
            }
        )

    def route_status(self, *responses):
        self.fake.add("GET", rf"/documento-electronico/status/{CDC}$", *responses)

    def route_files(self):
        for suffix, content, kind in (("xml", XML, "xml"), ("kude", PDF, "pdf")):
            self.fake.add(
                "GET",
                rf"/documento-electronico/{CDC}/{suffix}$",
                FakeResponse(
                    200,
                    content=content,
                    headers={"Content-Type": f"application/{kind}"},
                ),
            )

    def run_cron(self):
        self.env["account.move"]._cron_sifende_check_status()

    def outcome(self, move):
        return (move.l10n_py_edi_status, move.l10n_py_sifende_envio_fallido)

    # === Adaptador de OCA: check_status ===

    def check(self, response):
        self.route_status(response)
        return self.connector.check_status(CDC)

    def test_check_status_de_los_estados_de_sifende(self):
        for estado, success, status in (
            ("APROBADO", True, "Aprobado"),
            ("APROBADO_OBSERVACION", True, "Aprobado"),
            ("RECHAZADO", False, "Rechazado"),
            ("PENDIENTE", False, None),
            ("EN_LOTE", False, None),
            ("CANCELADO", False, None),
            ("ERROR", False, None),
        ):
            for extra in ({}, {"mensajeRechazo": "1001 - motivo"}):
                with self.subTest(estado=estado, extra=extra):
                    response = self.status_response(estado, **extra)
                    body = response.json()
                    expected_result = dict(
                        body, **({"status": status} if status else {})
                    )
                    expected = {"success": success, "result": expected_result}
                    if not success:
                        expected["error"] = extra.get("mensajeRechazo") or estado
                    self.assertEqual(self.check(response), expected)

    def test_check_status_no_modifica_el_cuerpo_de_la_respuesta(self):
        response = self.status_response("APROBADO")
        result = self.check(response)["result"]
        self.assertEqual(result["status"], "Aprobado")
        self.assertNotIn("status", response.json())

    def test_check_status_ante_una_consulta_fallida_no_trae_result(self):
        for name, (response, _logged_ok) in QUERY_FAILURES.items():
            if name == "200_sin_estado":
                continue
            with self.subTest(failure=name):
                result = self.check(response)
                self.assertFalse(result["success"])
                self.assertEqual(set(result), {"success", "error"})
                self.assertTrue(result["error"])

    def test_check_status_mensajes_de_error_de_la_consulta(self):
        self.assertEqual(
            self.check(requests.Timeout()),
            {"success": False, "error": "tiempo de espera agotado"},
        )
        self.assertEqual(
            self.check(problem("internal-error", 503, "no disponible")),
            {"success": False, "error": "no disponible"},
        )
        self.assertEqual(
            self.check(problem("unauthorized", 401)),
            {
                "success": False,
                "error": "SIFENDE rechazó la API key (401). Verifique que sea "
                "válida y no esté vencida.",
            },
        )
        self.assertEqual(
            self.check(FakeResponse(200, ["x"])),
            {"success": False, "error": "Error HTTP 200"},
        )

    def test_check_status_con_cuerpo_sin_estado_no_es_exitoso(self):
        # diferencia documentada del refactor: antes devolvía el cuerpo en `result`
        # y un `error` vacío; ahora es una consulta fallida, como cualquier otra.
        # Para OCA (que solo lee `success`) es lo mismo: no hay estado que aplicar
        for body in ({"cdc": CDC}, {}, {"estado": None, "cdc": CDC}):
            with self.subTest(body=body):
                self.assertEqual(
                    self.check(FakeResponse(200, body)),
                    {
                        "success": False,
                        "error": "Sifende no informó el estado del documento.",
                    },
                )

    # === _sifende_read_status ===

    def test_read_status_devuelve_el_estado(self):
        self.route_status(
            self.status_response("RECHAZADO", mensajeRechazo="1001 - motivo")
        )
        state = self.connector._sifende_read_status(CDC)
        self.assertIsInstance(state, DocumentState)
        self.assertEqual(
            (state.cdc, state.estado, state.message),
            (CDC, "RECHAZADO", "1001 - motivo"),
        )
        self.assertFalse(state.approved)
        self.assertEqual(state.raw["numeroDocumento"], 1)

    def test_read_status_levanta_status_unavailable_ante_una_consulta_fallida(self):
        for name, (response, _logged_ok) in QUERY_FAILURES.items():
            with self.subTest(failure=name):
                self.route_status(response)
                with self.assertRaises(StatusUnavailable):
                    self.connector._sifende_read_status(CDC)

    # === Garantía de H9 en el cron ===

    def test_una_consulta_fallida_no_cambia_nada_y_el_cron_la_sella(self):
        for name, (response, _logged_ok) in QUERY_FAILURES.items():
            for start in ("processing", "accepted", "failed"):
                with self.subTest(failure=name, start=start):
                    move = self.tracked(start, l10n_py_edi_message="mensaje previo")
                    # los archivos ya están: el cron no tiene otro motivo para llamar
                    move.write(
                        {
                            "l10n_py_edi_xml": "WE1M",
                            "l10n_py_kude_pdf": "UERG",
                        }
                    )
                    self.fake.calls.clear()
                    self.route_status(response)
                    self.assertFalse(move.l10n_py_sifende_ultima_consulta)
                    with self.assertLogs(ACCOUNT_MOVE_LOGGER, "INFO") as logs:
                        self.run_cron()
                    self.assertIn(
                        "Sin estado vigente en Sifende para el documento " + CDC,
                        "\n".join(logs.output),
                    )
                    self.assertEqual(self.outcome(move), STARTS[start])
                    self.assertEqual(move.l10n_py_edi_message, "mensaje previo")
                    self.assertEqual(len(self.fake.calls_to("GET", "/status/")), 1)
                    self.assertTrue(move.l10n_py_sifende_ultima_consulta)
                    self.release(move)

    def test_el_documento_en_proceso_sigue_en_el_proximo_lote_tras_una_falla(self):
        move = self.tracked("processing")
        self.route_status(requests.Timeout())
        self.run_cron()
        self.assertTrue(move.l10n_py_sifende_ultima_consulta)
        self.assertIn(
            move, self.env["account.move"]._l10n_py_sifende_to_sync(self.connector)
        )
        self.route_status(self.status_response("RECHAZADO", mensajeRechazo="1001"))
        self.run_cron()
        self.assertEqual(move.l10n_py_edi_status, "rejected")

    def test_una_consulta_fallida_no_impide_bajar_los_archivos_de_un_aceptado(self):
        for name, (response, _logged_ok) in QUERY_FAILURES.items():
            with self.subTest(failure=name):
                move = self.tracked("accepted")
                self.fake.calls.clear()
                self.route_status(response)
                self.route_files()
                self.run_cron()
                self.assertEqual(move.l10n_py_edi_status, "accepted")
                self.assertTrue(move.l10n_py_edi_xml)
                self.assertTrue(move.l10n_py_kude_pdf)
                self.assertFalse(move.l10n_py_sifende_archivos_pendientes)
                self.assertTrue(move.l10n_py_sifende_ultima_consulta)
                self.release(move)

    def test_una_cancelacion_se_aplica_cuando_la_consulta_vuelve(self):
        move = self.tracked("accepted")
        move.write({"l10n_py_edi_xml": "WE1M", "l10n_py_kude_pdf": "UERG"})
        self.route_status(requests.Timeout())
        self.run_cron()
        self.assertEqual(move.l10n_py_edi_status, "accepted")
        move.l10n_py_sifende_ultima_consulta = fields.Datetime.now() - timedelta(
            hours=2
        )
        self.route_status(self.status_response("CANCELADO"))
        self.run_cron()
        self.assertEqual(move.l10n_py_edi_status, "cancelled")

    # === Qué estados aplica el documento ===

    def test_aplicar_un_estado_remoto(self):
        for state, start in product(REMOTE_STATES, STARTS):
            with self.subTest(state=state, start=start):
                move = self.tracked(start)
                move._l10n_py_sifende_apply_state(state)
                self.assertEqual(
                    self.outcome(move), TRANSITIONS.get((state, start), STARTS[start])
                )
                self.release(move)

    def test_refrescar_aplica_el_estado_de_la_consulta(self):
        for state, start in product(REMOTE_STATES, STARTS):
            with self.subTest(state=state, start=start):
                move = self.tracked(start)
                self.route_status(self.status_response(state))
                move._l10n_py_sifende_refresh_state(self.connector)
                self.assertEqual(
                    self.outcome(move), TRANSITIONS.get((state, start), STARTS[start])
                )
                self.release(move)

    def test_refrescar_usa_el_mensaje_de_rechazo_de_la_consulta(self):
        move = self.tracked("processing")
        self.route_status(self.status_response("RECHAZADO", mensajeRechazo="1001 - x"))
        move._l10n_py_sifende_refresh_state(self.connector)
        self.assertTrue(move.l10n_py_edi_message.startswith("1001 - x\n\n"))

    def test_refrescar_prefiere_el_mensaje_del_aviso_si_coincide_el_resultado(self):
        for announced, state, expected in (
            ("APROBADO", "APROBADO_OBSERVACION", "aviso"),
            ("APROBADO", "APROBADO", "aviso"),
            ("RECHAZADO", "RECHAZADO", "aviso"),
            ("RECHAZADO", "APROBADO", "consulta"),
            ("APROBADO", "RECHAZADO", "consulta"),
        ):
            with self.subTest(announced=announced, state=state):
                move = self.tracked("processing")
                self.route_status(
                    self.status_response(state, mensajeRechazo="consulta")
                )
                move._l10n_py_sifende_refresh_state(self.connector, announced, "aviso")
                self.assertTrue(
                    move.l10n_py_edi_message.startswith(expected),
                    move.l10n_py_edi_message,
                )
                self.release(move)

    # === Qué documentos consultan estado ===

    def test_el_cron_consulta_solo_los_documentos_que_pueden_cambiar(self):
        for start in STARTS:
            with self.subTest(start=start):
                move = self.tracked(start)
                self.fake.calls.clear()
                self.route_status(self.status_response("PENDIENTE"))
                with patch.object(type(move), "_l10n_py_sifende_fetch_files"):
                    move._l10n_py_sifende_sync(self.connector, kude_wait=0)
                self.assertEqual(
                    len(self.fake.calls_to("GET", "/status/")),
                    int(start in ("processing", "accepted", "failed")),
                )
                self.release(move)

    def test_un_aviso_consulta_solo_los_documentos_que_pueden_cambiar(self):
        for event_type, start in product(
            ("documento.aprobado", "documento.rechazado", "documento.cancelado"),
            STARTS,
        ):
            with self.subTest(event_type=event_type, start=start):
                move = self.tracked(start)
                self.fake.calls.clear()
                self.route_status(self.status_response("PENDIENTE"))
                with patch.object(type(move), "_l10n_py_sifende_fetch_files"):
                    move._l10n_py_sifende_apply_webhook(
                        self.connector, event_type, {"cdc": CDC}
                    )
                self.assertEqual(
                    len(self.fake.calls_to("GET", "/status/")),
                    int(
                        start in ("processing", "failed")
                        or (start == "accepted" and event_type == "documento.cancelado")
                    ),
                )
                self.release(move)


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestDocumentStateValue(BaseCase):
    def test_un_cuerpo_sin_estado_no_es_un_estado(self):
        for body in (
            None,
            [],
            "APROBADO",
            {},
            {"cdc": CDC},
            {"estado": None},
            {"estado": ""},
        ):
            with self.subTest(body=body):
                self.assertIsNone(DocumentState.from_body(CDC, body))

    def test_un_estado_conserva_el_mensaje_y_copia_el_cuerpo(self):
        body = {"cdc": CDC, "estado": "RECHAZADO", "mensajeRechazo": "1001"}
        state = DocumentState.from_body(CDC, body)
        self.assertEqual(
            (state.cdc, state.estado, state.message), (CDC, "RECHAZADO", "1001")
        )
        self.assertEqual(state.raw, body)
        self.assertIsNot(state.raw, body)
        self.assertIsNone(DocumentState.from_body(CDC, {"estado": "ERROR"}).message)

    def test_solo_los_aprobados_estan_aprobados(self):
        for estado in ("APROBADO", "APROBADO_OBSERVACION", "RECHAZADO", "PENDIENTE"):
            with self.subTest(estado=estado):
                self.assertEqual(
                    DocumentState.from_body(CDC, {"estado": estado}).approved,
                    estado.startswith("APROBADO"),
                )
