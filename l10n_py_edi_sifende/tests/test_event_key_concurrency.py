import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from random import randint
from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.orm.registry import DummyRLock, Registry
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.l10n_py_edi_sifende.services import event_key

MODEL = "l10n_py.number.inutilization"


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestEventKeyConcurrency(TransactionCase):
    """La clave del evento se reserva con transacciones reales de PostgreSQL.

    La clase no entra en el modo de prueba del registro: `registry.cursor()` abre
    conexiones independientes, como en producción. Por eso el registro de prueba se
    confirma y se borra a mano al terminar.
    """

    def setUp(self):
        super().setUp()
        # el cargador de pruebas retiene el candado del registro: sin esto, los hilos
        # de abajo no podrían ni crear su entorno
        self.enterContext(patch.object(Registry, "_lock", DummyRLock()))
        today = date.today()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            authorization = env["account.authorization"].create(
                {
                    "name": str(randint(10_000_000, 99_999_999)),
                    "date_from": today - timedelta(days=30),
                    "date_to": today + timedelta(days=335),
                    "invoice_number_from": 1,
                    "invoice_number_to": 9999999,
                    "establishment": "001",
                    "expedition_point": "001",
                    "l10n_latam_document_type_id": env.ref(
                        "l10n_py_account.dc_py_f"
                    ).id,
                    "company_id": env.company.id,
                }
            )
            inutilization = env[MODEL].create(
                {
                    "authorization_id": authorization.id,
                    "number_from": 5,
                    "number_to": 7,
                    "motive": "Números saltados por error de sistema",
                }
            )
            self.authorization_id = authorization.id
            self.inutilization_id = inutilization.id
        self.addCleanup(self._drop_records)

    def _drop_records(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env[MODEL].browse(self.inutilization_id).unlink()
            env["account.authorization"].browse(self.authorization_id).unlink()

    def _stored(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[MODEL].browse(self.inutilization_id)
            return (
                record.l10n_py_sifende_event_key,
                record.l10n_py_sifende_event_request,
            )

    def _attempt(self, label, build_request=None, commit=True):
        """Una transacción completa, como la de un pedido de usuario."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[MODEL].browse(self.inutilization_id)
            result = event_key.get_or_create(
                record, build_request or (lambda: {"motivo": label})
            )
            if not commit:
                cr.rollback()
            return result

    def test_dos_transacciones_simultaneas_comparten_una_clave_durable(self):
        # ambas leen la clave vacía y arman su solicitud antes de que alguna reserve
        barrier = threading.Barrier(2, timeout=10)

        def build_request_of(label):
            def build_request():
                # si la otra transacción nunca llega, la barrera se rompe y la
                # prueba falla de forma visible
                barrier.wait()
                return {"motivo": label}

            return build_request

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._attempt, label, build_request_of(label))
                for label in ("Motivo A", "Motivo B")
            ]
            (key_a, request_a), (key_b, request_b) = [f.result() for f in futures]
        self.assertEqual(key_a, key_b, "se generaron dos claves para un mismo evento")
        self.assertEqual(request_a, request_b)
        stored_key, stored_request = self._stored()
        self.assertEqual(stored_key, key_a)
        self.assertEqual(json.loads(stored_request), request_a)

    def test_una_transaccion_con_lectura_vieja_no_pisa_la_clave_ya_reservada(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[MODEL].browse(self.inutilization_id)
            # la lectura fija la instantánea de esta transacción: todavía sin clave
            self.assertFalse(record.l10n_py_sifende_event_key)
            first = self._attempt("Motivo A")
            second = event_key.get_or_create(record, lambda: {"motivo": "Motivo B"})
        self.assertEqual(first, second)
        self.assertEqual(second[1], {"motivo": "Motivo A"})
        self.assertEqual(self._stored()[0], first[0])

    def test_tras_un_rollback_completo_otra_transaccion_recupera_la_misma_clave(self):
        key, request = self._attempt("Motivo A", commit=False)

        def must_not_build():
            raise AssertionError("la solicitud reservada no se vuelve a armar")

        self.assertEqual(self._attempt("Motivo B", must_not_build), (key, request))
        self.assertEqual(self._stored()[0], key)

    def test_la_reserva_no_espera_el_bloqueo_que_tiene_la_misma_transaccion(self):
        # crear en la misma transacción un registro que apunta a la fila (el asistente
        # de cancelación, por ejemplo) deja un bloqueo KEY SHARE que nadie libera
        # mientras la reserva espera
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[MODEL].browse(self.inutilization_id)
            cr.execute(
                f"SELECT id FROM {record._table} WHERE id = %s FOR KEY SHARE",
                [record.id],
            )
            result = {}
            worker = threading.Thread(
                target=lambda: result.update(
                    value=event_key.get_or_create(
                        record, lambda: {"motivo": "Motivo A"}
                    )
                )
            )
            worker.start()
            worker.join(timeout=10)
            if worker.is_alive():
                # liberar el bloqueo para que el hilo termine antes de fallar
                cr.rollback()
                worker.join()
                self.fail("la reserva quedó esperando el bloqueo de quien la pidió")
        self.assertEqual(result["value"][1], {"motivo": "Motivo A"})
        self.assertEqual(self._stored()[0], result["value"][0])

    def test_un_rechazo_definitivo_descarta_la_clave_aunque_la_lectura_sea_vieja(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            record = env[MODEL].browse(self.inutilization_id)
            # la instantánea de esta transacción es anterior a la reserva
            self.assertFalse(record.l10n_py_sifende_event_key)
            event_key.get_or_create(record, lambda: {"motivo": "Motivo A"})
            event_key.forget(record, committed=True)
        self.assertEqual(self._stored(), (False, False))
