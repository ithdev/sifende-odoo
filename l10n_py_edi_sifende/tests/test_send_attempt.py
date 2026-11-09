import contextlib
import json

import requests

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import API_KEY, FakeResponse, SifendeCommon, problem

DOCUMENT_URL = r"^/documento-electronico$"

# resultado de un envío (qué contesta Sifende, o qué pasa antes de llegar a él)
CONFIRMED = "confirmed"
HTTP_503 = "503"
TIMEOUT = "timeout"
DEFINITIVE = "definitive"
SETUP = "setup"
IN_USE = "in_use"
NOT_SENT = "not_sent"
# un 409 sin identificar: para un documento es definitivo
CONFLICT = "conflict"

UNCERTAIN = (HTTP_503, TIMEOUT)

# (status, bandera, solicitud guardada, número ocupado) de cada resultado, venga el
# envío del estado que venga; NOT_SENT depende del estado y se calcula aparte
TRANSITIONS = {
    CONFIRMED: ("processing", False, False, False),
    HTTP_503: ("error", True, True, False),
    TIMEOUT: ("error", True, True, False),
    DEFINITIVE: ("rejected", False, False, False),
    SETUP: ("error", False, False, False),
    IN_USE: ("error", False, False, True),
    CONFLICT: ("error", False, False, False),
}


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestSendAttempt(SifendeCommon):
    """Fija las transiciones del intento de envío de un documento.

    Cada envío termina en un resultado confirmado, incierto, definitivo o ``no
    enviado`` (falla antes de hablar con Sifende); la bandera de reintento con la
    misma clave y la solicitud guardada solo se escriben juntas, y el intento sube
    únicamente al empezar uno nuevo tras uno cerrado.
    """

    def apply(self, outcome):
        """Prepara lo que Sifende contestará al próximo POST (o la falla previa)."""
        self.connector.sifende_api_key = False if outcome == NOT_SENT else API_KEY
        responses = {
            CONFIRMED: lambda: self.emission_response(),
            HTTP_503: lambda: problem("internal-error", 503),
            TIMEOUT: lambda: requests.Timeout(),
            DEFINITIVE: lambda: problem("validation-error", 400, "Dato inválido"),
            SETUP: lambda: problem("timbrado-no-vigente", 422, "Detalle"),
            IN_USE: lambda: problem("document-number-in-use", 409, "Número en uso"),
            CONFLICT: lambda: problem("otro-conflicto", 409, "Conflicto"),
        }
        if outcome != NOT_SENT:
            self.route_emission(responses[outcome]())

    def posts(self):
        return self.fake.calls_to("POST", DOCUMENT_URL)

    def snapshot(self, move):
        return (
            move.l10n_py_edi_status,
            move.l10n_py_sifende_reintento_misma_clave,
            bool(move.sudo().l10n_py_sifende_request),
            move.l10n_py_sifende_intento,
            move.l10n_py_sifende_envio_fallido,
            move.l10n_py_sifende_numero_ocupado,
        )

    def edit_sources(self):
        # la calle termina en un número de casa: la etiqueta es numérica
        self._edits = getattr(self, "_edits", 0) + 1
        tag = self._edits
        self.partner.write(
            {
                "name": f"Cliente {tag} S.A.",
                "email": f"cliente{tag}@cliente.com.py",
                "street": f"Calle {tag}",
                "ref": f"CLI-{tag}",
            }
        )
        self.product.write({"default_code": f"COD-{tag}"})

    # === Estados de partida ===

    def move_new(self):
        return self.make_move("fe")

    def move_open(self):
        move = self.make_move("fe")
        self.apply(HTTP_503)
        move.action_send_edi()
        return move

    def move_closed(self):
        move = self.make_move("fe")
        self.apply(DEFINITIVE)
        move.action_send_edi()
        return move

    def move_closed_error(self):
        move = self.make_move("fe")
        self.apply(SETUP)
        move.action_send_edi()
        return move

    def move_legacy(self):
        # fila anterior a guardar la solicitud: bandera encendida y sin cuerpo
        move = self.move_open()
        move.sudo().l10n_py_sifende_request = False
        return move

    STARTS = {
        "new": ("move_new", False),
        "open": ("move_open", False),
        "closed": ("move_closed", True),
        "closed_error": ("move_closed_error", True),
        "legacy": ("move_legacy", False),
    }

    # === T1: tabla de transiciones ===

    def expected_row(self, start, outcome, before):
        status, flag, request, occupied = TRANSITIONS.get(outcome, (None,) * 4)
        renewed = self.STARTS[start][1]
        intento = before[3] + (1 if renewed else 0)
        if outcome == NOT_SENT:
            # nada llegó a Sifende: un intento incierto abierto sigue abierto
            flag = before[1]
            request = before[2]
            status, occupied = "error", False
        return (status, flag, request, intento, False, occupied)

    def test_transiciones_del_envio_segun_el_estado_de_partida_y_el_resultado(self):
        outcomes = (*TRANSITIONS, NOT_SENT)
        for start, (builder, _renews) in self.STARTS.items():
            for outcome in outcomes:
                with self.subTest(start=start, outcome=outcome):
                    with contextlib.closing(self.env.cr.savepoint()):
                        move = getattr(self, builder)()
                        before = self.snapshot(move)
                        self.apply(outcome)
                        move.action_send_edi()
                        self.assertEqual(
                            self.snapshot(move),
                            self.expected_row(start, outcome, before),
                        )

    def test_un_envio_cuyo_resultado_es_incierto_guarda_el_cuerpo_enviado(self):
        for outcome in UNCERTAIN:
            with self.subTest(outcome=outcome):
                move = self.make_move("fe")
                self.apply(outcome)
                move.action_send_edi()
                sent = self.posts()[-1]
                self.assertEqual(
                    json.loads(move.sudo().l10n_py_sifende_request), sent.json
                )

    def test_un_envio_confirmado_o_definitivo_no_deja_solicitud_ni_bandera(self):
        for outcome in (CONFIRMED, DEFINITIVE, SETUP, IN_USE, CONFLICT):
            with self.subTest(outcome=outcome):
                move = self.move_open()
                self.apply(outcome)
                move.action_send_edi()
                self.assertFalse(move.l10n_py_sifende_reintento_misma_clave)
                self.assertFalse(move.sudo().l10n_py_sifende_request)

    def test_el_estado_envio_fallido_de_sifende_impide_enviar_sin_tocar_nada(self):
        move = self.make_move("fe")
        move.write(
            {"l10n_py_edi_status": "error", "l10n_py_sifende_envio_fallido": True}
        )
        before = self.snapshot(move)
        self.apply(CONFIRMED)
        for step in (move.action_send_edi, move.action_retry_edi):
            with self.subTest(step=step.__name__):
                with self.assertRaises(UserError):
                    step()
                self.assertEqual(self.snapshot(move), before)
        self.assertFalse(self.posts())

    def test_un_error_de_acceso_previo_al_post_se_trata_como_no_enviado(self):
        # la frontera que sostiene NOT_SENT: send_document solo levanta UserError
        # antes de hablar con Sifende
        move = self.make_move("fe")
        self.apply(NOT_SENT)
        move.action_send_edi()
        self.assertEqual(move.l10n_py_edi_status, "error")
        self.assertIn("API key", move.l10n_py_edi_message)
        self.assertFalse(self.posts())

    def test_un_409_sin_identificar_es_definitivo_para_un_documento(self):
        move = self.move_open()
        self.apply(CONFLICT)
        move.action_send_edi()
        self.assertFalse(move.l10n_py_sifende_reintento_misma_clave)
        self.assertFalse(move.sudo().l10n_py_sifende_request)

    def test_la_idempotencia_en_curso_es_incierta_con_su_409(self):
        move = self.make_move("fe")
        self.route_emission(problem("idempotency-in-progress", 409))
        move.action_send_edi()
        self.assertTrue(move.l10n_py_sifende_reintento_misma_clave)
        self.assertTrue(move.sudo().l10n_py_sifende_request)

    def test_los_estados_http_transitorios_son_inciertos(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                move = self.make_move("fe")
                self.route_emission(FakeResponse(status, {}))
                move.action_send_edi()
                self.assertTrue(move.l10n_py_sifende_reintento_misma_clave)
                self.assertTrue(move.sudo().l10n_py_sifende_request)

    def test_los_estados_http_definitivos_no_dejan_el_intento_abierto(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                move = self.make_move("fe")
                self.route_emission(FakeResponse(status, {}))
                move.action_send_edi()
                self.assertFalse(move.l10n_py_sifende_reintento_misma_clave)
                self.assertFalse(move.sudo().l10n_py_sifende_request)

    # === T2: una sola solicitud por clave, entre todos los accesos al envío ===

    def paths(self, move):
        log = self.env["l10n_py.edi.log"].create(
            {"operation_type": "send", "document_id": move.id, "provider": "sifende"}
        )
        wizard = self.env["account.move.send.edi.wizard"].create(
            {"invoice_id": move.id}
        )
        return {
            "action_send_edi": move.action_send_edi,
            "action_retry_edi": move.action_retry_edi,
            "send_wizard": wizard.action_send,
            "log_retry_operation": log.action_retry_operation,
        }

    PATH_NAMES = (
        "action_send_edi",
        "action_retry_edi",
        "send_wizard",
        "log_retry_operation",
    )

    def test_todos_los_accesos_reenvian_la_misma_solicitud_con_el_intento_abierto(self):
        for name in self.PATH_NAMES:
            with self.subTest(path=name):
                move = self.move_open()
                first = self.posts()[-1]
                before = len(self.posts())
                paths = self.paths(move)
                for outcome in (HTTP_503, CONFIRMED):
                    self.edit_sources()
                    self.apply(outcome)
                    paths[name]()
                new = self.posts()[before:]
                self.assertTrue(new)
                self.assertEqual(
                    {c.headers["Idempotency-Key"] for c in new},
                    {first.headers["Idempotency-Key"]},
                )
                self.assertEqual({repr(c.json) for c in new}, {repr(first.json)})
                self.assertEqual(move.l10n_py_edi_status, "processing")
                self.assertFalse(move.sudo().l10n_py_sifende_request)

    def test_todos_los_accesos_abren_un_intento_nuevo_con_otra_solicitud_tras_cerrar(
        self,
    ):
        for name in self.PATH_NAMES:
            with self.subTest(path=name):
                move = self.move_closed()
                first = self.posts()[-1]
                before = len(self.posts())
                paths = self.paths(move)
                self.edit_sources()
                self.apply(CONFIRMED)
                paths[name]()
                new = self.posts()[before:]
                self.assertEqual(len(new), 1)
                self.assertNotEqual(
                    new[0].headers["Idempotency-Key"], first.headers["Idempotency-Key"]
                )
                self.assertNotEqual(new[0].json, first.json)
                self.assertEqual(move.l10n_py_edi_status, "processing")

    # === T5: el número de intento ===

    def test_el_intento_sube_uno_por_renovacion_y_cero_con_el_intento_abierto(self):
        for name in self.PATH_NAMES:
            for start, (builder, renews) in self.STARTS.items():
                if start == "new" and name == "action_retry_edi":
                    continue  # OCA solo reintenta documentos con error o rechazados
                with self.subTest(path=name, start=start):
                    with contextlib.closing(self.env.cr.savepoint()):
                        move = getattr(self, builder)()
                        before = move.l10n_py_sifende_intento
                        paths = self.paths(move)
                        self.apply(CONFIRMED)
                        paths[name]()
                        self.assertEqual(
                            move.l10n_py_sifende_intento, before + (1 if renews else 0)
                        )

    def test_renumerar_y_volver_a_enviar_sube_el_intento_una_sola_vez(self):
        move = self.make_move("fe")
        self.apply(IN_USE)
        move.action_send_edi()
        self.assertEqual(move.l10n_py_sifende_intento, 0)
        move.action_l10n_py_sifende_renumber()
        self.assertEqual(move.l10n_py_edi_status, "to_send")
        self.assertEqual(move.l10n_py_sifende_intento, 1)
        self.apply(CONFIRMED)
        move.action_send_edi()
        self.assertEqual(move.l10n_py_sifende_intento, 1)
        keys = [c.headers["Idempotency-Key"] for c in self.posts()]
        self.assertEqual([k.rsplit(":", 1)[1] for k in keys], ["0", "1"])

    def test_restablecer_a_borrador_tras_un_cierre_sube_el_intento_una_sola_vez(self):
        move = self.move_closed()
        move.button_draft()
        self.assertEqual(move.l10n_py_sifende_intento, 1)
        move.action_post()
        self.apply(CONFIRMED)
        move.action_send_edi()
        self.assertEqual(move.l10n_py_sifende_intento, 1)

    def test_restablecer_a_borrador_sin_cierre_no_sube_el_intento(self):
        move = self.make_move("fe")
        move.button_draft()
        self.assertEqual(move.l10n_py_sifende_intento, 0)

    # === T4: secuencias enumeradas ===

    ALPHABET = (HTTP_503, TIMEOUT, DEFINITIVE, IN_USE, NOT_SENT, CONFIRMED)
    DEPTH = 3

    def check_invariants(self, move, calls, step_posts, previous):
        """Una solicitud por clave; bandera si y solo si hay solicitud guardada.

        ``previous`` es (clave, resultado) del POST anterior del recorrido.
        """
        by_key = {}
        for call in calls:
            by_key.setdefault(call.headers["Idempotency-Key"], set()).add(
                json.dumps(call.json)
            )
        for key, bodies in by_key.items():
            self.assertEqual(len(bodies), 1, f"{key} salió con cuerpos distintos")
        flag = move.l10n_py_sifende_reintento_misma_clave
        request = move.sudo().l10n_py_sifende_request
        self.assertEqual(flag, bool(request))
        if request:
            self.assertEqual(json.loads(request), calls[-1].json)
        keys = {c.headers["Idempotency-Key"] for c in step_posts}
        self.assertLessEqual(len(keys), 1, "un envío usó dos claves")
        if step_posts and previous:
            previous_key, previous_outcome = previous
            if previous_outcome in UNCERTAIN:
                self.assertEqual(keys, {previous_key})
            else:
                self.assertNotIn(previous_key, keys)

    def explore(self, move, depth, previous, path):
        for outcome in self.ALPHABET:
            with contextlib.closing(self.env.cr.savepoint()):
                known = len(self.fake.calls)
                self.edit_sources()
                self.apply(outcome)
                posts_before = len(self.posts())
                if (
                    move.l10n_py_edi_status in ("error", "rejected")
                    and not move.l10n_py_sifende_numero_ocupado
                ):
                    move.action_retry_edi()
                else:
                    move.action_send_edi()
                calls = self.posts()
                step_posts = calls[posts_before:]
                label = " > ".join((*path, outcome))
                with self.subTest(sequence=label):
                    self.check_invariants(move, calls, step_posts, previous)
                    self.check_outcome(move, outcome)
                if outcome != CONFIRMED and depth + 1 < self.DEPTH:
                    last = (
                        (step_posts[-1].headers["Idempotency-Key"], outcome)
                        if step_posts
                        else previous
                    )
                    self.explore(move, depth + 1, last, (*path, outcome))
                del self.fake.calls[known:]

    def check_outcome(self, move, outcome):
        status = move.l10n_py_edi_status
        flag = move.l10n_py_sifende_reintento_misma_clave
        if outcome == CONFIRMED:
            self.assertEqual((status, flag), ("processing", False))
        elif outcome in UNCERTAIN:
            self.assertEqual((status, flag), ("error", True))
        elif outcome == DEFINITIVE:
            self.assertEqual((status, flag), ("rejected", False))
        elif outcome == IN_USE:
            self.assertEqual((status, flag), ("error", False))
            self.assertTrue(move.l10n_py_sifende_numero_ocupado)

    def test_ninguna_secuencia_de_resultados_envia_dos_cuerpos_con_la_misma_clave(self):
        move = self.make_move("fe")
        self.explore(move, 0, None, ())
