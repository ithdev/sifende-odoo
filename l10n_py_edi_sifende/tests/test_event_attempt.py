import contextlib
import json

import requests

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import CDC, FakeResponse, SifendeCommon, problem

CANCEL_URL = rf"/documento-electronico/{CDC}/cancelar$"
INUTILIZE_URL = r"/documento-electronico/inutilizar$"
OCA_INUTILIZATION_LOGGER = (
    "odoo.addons.l10n_py_edi_base.models.l10n_py_number_inutilization"
)

APPROVED = "approved"
REJECTED = "rejected"
PENDING = "pending"
NO_STATE = "no_state"
HTTP_503 = "503"
HTTP_429 = "429"
HTTP_500 = "500"
HTTP_502 = "502"
HTTP_504 = "504"
TIMEOUT = "timeout"
CONFLICT = "409"
IN_PROGRESS = "in_progress"
HTTP_400 = "400"
HTTP_401 = "401"
HTTP_404 = "404"
HTTP_422 = "422"

RESPONSES = {
    APPROVED: lambda: FakeResponse(200, {"estadoEvento": "APROBADO"}),
    REJECTED: lambda: FakeResponse(
        200, {"estadoEvento": "RECHAZADO", "mensajeRespuesta": "[4003] Plazo"}
    ),
    PENDING: lambda: FakeResponse(200, {"estadoEvento": "PENDIENTE"}),
    NO_STATE: lambda: FakeResponse(200, {}),
    HTTP_503: lambda: problem("internal-error", 503),
    HTTP_429: lambda: FakeResponse(429, {}),
    HTTP_500: lambda: FakeResponse(500, {}),
    HTTP_502: lambda: FakeResponse(502, {}),
    HTTP_504: lambda: FakeResponse(504, {}),
    TIMEOUT: lambda: requests.Timeout(),
    # un evento ya pendiente: Sifende lo está procesando
    CONFLICT: lambda: problem("evento-pendiente", 409),
    IN_PROGRESS: lambda: problem("idempotency-in-progress", 409),
    HTTP_400: lambda: problem("validation-error", 400, "Dato inválido"),
    HTTP_401: lambda: FakeResponse(401, {}),
    HTTP_404: lambda: FakeResponse(404, {}),
    HTTP_422: lambda: problem("evento-error", 422, "Rango en uso"),
}
# el evento conserva la clave y la solicitud
UNCERTAIN = (
    PENDING,
    NO_STATE,
    HTTP_503,
    HTTP_429,
    HTTP_500,
    HTTP_502,
    HTTP_504,
    TIMEOUT,
    CONFLICT,
    IN_PROGRESS,
)
# el evento las descarta, con la transacción del llamador o aparte
DEFINITIVE = (REJECTED, HTTP_400, HTTP_401, HTTP_404, HTTP_422)


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestEventAttempt(SifendeCommon):
    """Fija las transiciones del intento de un evento (cancelación, inutilización).

    Un evento reserva su clave y su solicitud antes del POST: la reserva se descarta
    cuando el resultado es confirmado o definitivo y se conserva cuando es incierto.
    La diferencia con los documentos es el 409: un evento ya pendiente es incierto.
    """

    def setUp(self):
        super().setUp()
        mute = mute_logger(OCA_INUTILIZATION_LOGGER)
        mute.__enter__()
        self.addCleanup(mute.__exit__, None, None, None)
        self._edits = 0

    # === Los dos eventos ===

    def accepted_move(self):
        move = self.make_move("fe", invoice_date=fields.Date.today())
        move.write({"l10n_py_edi_status": "accepted", "l10n_py_cdc": CDC})
        return move

    def inutilization(self):
        self.auths["fe"].series = "AB"
        return self.env["l10n_py.number.inutilization"].create(
            {
                "authorization_id": self.auths["fe"].id,
                "number_from": 5,
                "number_to": 7,
                "motive": "Números saltados por error de sistema",
            }
        )

    def route(self, target, outcome):
        url = CANCEL_URL if target == "cancel" else INUTILIZE_URL
        self.fake.add("POST", url, RESPONSES[outcome]())

    def posts(self, target):
        url = CANCEL_URL if target == "cancel" else INUTILIZE_URL
        return self.fake.calls_to("POST", url)

    def fails(self, method):
        # sin assertRaises: su savepoint desharía la clave confirmada aparte
        try:
            method()
        except UserError:
            return True
        return False

    def make_event(self, target):
        return self.accepted_move() if target == "cancel" else self.inutilization()

    def step(self, target, record):
        """Un envío del evento tal como lo hace el usuario; True si falló."""
        self._edits += 1
        if target == "cancel":
            wizard = self.env["l10n_py.edi.cancel.wizard"].create(
                {
                    "invoice_id": record.id,
                    "motive": f"Motivo número {self._edits} del usuario",
                }
            )
            failed = self.fails(wizard.action_cancel)
        else:
            # el timbrado no está bloqueado por el evento pendiente
            self.auths["fe"].series = ("AB", "CD", "EF", "GH", "IJ", "KL")[
                self._edits % 6
            ]
            failed = self.fails(record.action_send)
            if failed:
                record.state = "draft"
        return failed

    def pending(self, record):
        return (
            bool(record.l10n_py_sifende_event_key),
            bool(record.l10n_py_sifende_event_request),
        )

    TARGETS = ("cancel", "inutilization")

    # === T1: tabla de transiciones ===

    def test_el_resultado_decide_si_el_evento_conserva_la_clave_y_la_solicitud(self):
        for target in self.TARGETS:
            for outcome in (APPROVED, *DEFINITIVE, *UNCERTAIN):
                with self.subTest(target=target, outcome=outcome):
                    with contextlib.closing(self.env.cr.savepoint()):
                        record = self.make_event(target)
                        self.route(target, outcome)
                        failed = self.step(target, record)
                        self.assertEqual(failed, outcome != APPROVED)
                        keeps = outcome in UNCERTAIN
                        self.assertEqual(self.pending(record), (keeps, keeps))

    def test_tras_un_resultado_incierto_el_reintento_reusa_la_clave_y_la_solicitud(
        self,
    ):
        for target in self.TARGETS:
            for outcome in UNCERTAIN:
                with self.subTest(target=target, outcome=outcome):
                    with contextlib.closing(self.env.cr.savepoint()):
                        record = self.make_event(target)
                        self.route(target, outcome)
                        self.step(target, record)
                        first = self.posts(target)[-1]
                        self.route(target, APPROVED)
                        self.assertFalse(self.step(target, record))
                        retry = self.posts(target)[-1]
                        self.assertIsNot(retry, first)
                        self.assertEqual(
                            retry.headers["Idempotency-Key"],
                            first.headers["Idempotency-Key"],
                        )
                        self.assertEqual(retry.json, first.json)
                        self.assertEqual(self.pending(record), (False, False))

    def test_tras_un_resultado_definitivo_el_reintento_usa_otra_clave(self):
        for target in self.TARGETS:
            for outcome in DEFINITIVE:
                with self.subTest(target=target, outcome=outcome):
                    with contextlib.closing(self.env.cr.savepoint()):
                        self.fake.calls.clear()
                        record = self.make_event(target)
                        self.route(target, HTTP_503)
                        self.step(target, record)
                        self.route(target, outcome)
                        self.step(target, record)
                        self.route(target, APPROVED)
                        self.step(target, record)
                        keys = [
                            c.headers["Idempotency-Key"] for c in self.posts(target)
                        ]
                        # el rechazo definitivo reintentó con la clave pendiente
                        self.assertEqual(set(keys[:-1]), {keys[0]})
                        self.assertNotEqual(keys[-1], keys[0])

    def test_una_fila_con_clave_y_sin_solicitud_conserva_la_clave_y_guarda_la_solicitud(
        self,
    ):
        # filas anteriores a guardar la solicitud junto con la clave
        for target in self.TARGETS:
            with self.subTest(target=target):
                record = self.make_event(target)
                record.sudo().l10n_py_sifende_event_key = "clave-heredada"
                self.route(target, HTTP_503)
                self.step(target, record)
                call = self.posts(target)[-1]
                self.assertEqual(call.headers["Idempotency-Key"], "clave-heredada")
                self.assertEqual(
                    json.loads(record.l10n_py_sifende_event_request), call.json
                )

    # === T2: una sola solicitud por clave, entre los accesos a la cancelación ===

    def test_los_dos_accesos_a_la_cancelacion_reenvian_el_motivo_original(self):
        for name in ("action_cancel_edi", "wizard"):
            with self.subTest(path=name):
                move = self.accepted_move()
                self.route("cancel", HTTP_503)
                wizard = self.env["l10n_py.edi.cancel.wizard"].create(
                    {"invoice_id": move.id, "motive": "Error en el monto facturado"}
                )
                self.assertTrue(self.fails(wizard.action_cancel))
                first = self.posts("cancel")[-1]
                self.route("cancel", APPROVED)
                if name == "wizard":
                    self.env["l10n_py.edi.cancel.wizard"].create(
                        {"invoice_id": move.id, "motive": "Otro motivo distinto"}
                    ).action_cancel()
                else:
                    move.action_cancel_edi()
                retry = self.posts("cancel")[-1]
                self.assertEqual(
                    retry.headers["Idempotency-Key"], first.headers["Idempotency-Key"]
                )
                self.assertEqual(retry.json, {"motivo": "Error en el monto facturado"})
                self.assertEqual(move.l10n_py_edi_status, "cancelled")

    # === T4: secuencias enumeradas ===

    ALPHABET = (HTTP_503, TIMEOUT, CONFLICT, PENDING, HTTP_400, REJECTED, APPROVED)
    DEPTH = 3

    def check_invariants(self, target, record, outcome, step_posts, previous):
        calls = self.posts(target)
        by_key = {}
        for call in calls:
            by_key.setdefault(call.headers["Idempotency-Key"], set()).add(
                json.dumps(call.json)
            )
        for key, bodies in by_key.items():
            self.assertEqual(len(bodies), 1, f"{key} salió con cuerpos distintos")
        keeps = outcome in UNCERTAIN
        self.assertEqual(self.pending(record), (keeps, keeps))
        if keeps:
            self.assertEqual(
                json.loads(record.l10n_py_sifende_event_request), calls[-1].json
            )
            self.assertEqual(record.l10n_py_sifende_event_key, self.last_key(calls))
        keys = {c.headers["Idempotency-Key"] for c in step_posts}
        self.assertEqual(len(keys), 1, "un envío usó dos claves")
        if previous:
            previous_key, previous_outcome = previous
            if previous_outcome in UNCERTAIN:
                self.assertEqual(keys, {previous_key})
            else:
                self.assertNotIn(previous_key, keys)

    @staticmethod
    def last_key(calls):
        return calls[-1].headers["Idempotency-Key"]

    def explore(self, target, record, depth, previous, path):
        for outcome in self.ALPHABET:
            with contextlib.closing(self.env.cr.savepoint()):
                known = len(self.fake.calls)
                posts_before = len(self.posts(target))
                self.route(target, outcome)
                self.step(target, record)
                step_posts = self.posts(target)[posts_before:]
                label = " > ".join((*path, outcome))
                with self.subTest(target=target, sequence=label):
                    self.check_invariants(target, record, outcome, step_posts, previous)
                if outcome != APPROVED and depth + 1 < self.DEPTH:
                    last = (self.last_key(step_posts), outcome)
                    self.explore(target, record, depth + 1, last, (*path, outcome))
                del self.fake.calls[known:]

    def test_ninguna_secuencia_de_resultados_envia_dos_cuerpos_con_la_misma_clave(self):
        for target in self.TARGETS:
            self.fake.calls.clear()
            self.explore(target, self.make_event(target), 0, None, ())

    def test_un_envio_de_inutilizacion_pendiente_bloquea_los_cambios_a_su_solicitud(
        self,
    ):
        inutilization = self.inutilization()
        self.route("inutilization", HTTP_503)
        self.step("inutilization", inutilization)
        with self.assertRaises(UserError):
            inutilization.write({"motive": "Otro motivo distinto del original"})
        inutilization.write({"motive": inutilization.motive})
