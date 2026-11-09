import json
import uuid

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.tools import SQL

from .client import SEND_CONFIRMED, SEND_DEFINITIVE

FIELDS = ("l10n_py_sifende_event_key", "l10n_py_sifende_event_request")
RESERVE_ATTEMPTS = 3


def get_or_create(record, build_request):
    """Return the idempotency key and the request it is bound to.

    The same key must always reach Sifende with the same body, so the request is
    stored with the key and reused while the event is pending. `build_request` is
    only called when there is nothing stored yet.
    """
    if not record:
        return str(uuid.uuid4()), build_request()
    record = record.sudo()
    key, stored = record.l10n_py_sifende_event_key, record.l10n_py_sifende_event_request
    if key and stored:
        return key, json.loads(stored)
    request = build_request()
    key, stored = _reserve(record, key or str(uuid.uuid4()), json.dumps(request))
    return key, json.loads(stored)


def settle(record, outcome):
    """End an event send with the same outcomes as a document's attempt.

    Confirmed: the key and the request go with the caller's transaction, which
    also changes the state of the record. Definitive: they go in another cursor,
    because OCA raises a UserError and its rollback would bring them back.
    Uncertain, or not sent: they stay reserved for the retry.
    """
    if outcome == SEND_CONFIRMED:
        forget(record, committed=False)
    elif outcome == SEND_DEFINITIVE:
        forget(record, committed=True)


def forget(record, committed):
    # sin mirar la clave antes: la transacción que llama puede tener una lectura
    # anterior a la reserva, que se confirmó en otro cursor
    if record:
        _write(record, dict.fromkeys(FIELDS, False), committed)


def pending_request(record):
    stored = record.sudo().l10n_py_sifende_event_request if record else False
    return json.loads(stored) if stored else {}


def _reserve(record, key, request):
    """Store the key and request unless another transaction already did.

    Two transactions can read an empty key at the same time. The row is locked in
    another cursor, so that the rollback of the error the user sees cannot lose the
    reservation, and what is stored there wins over what the caller read. Whoever
    arrives second gets the key and request of the first. The lock is FOR NO KEY
    UPDATE: it still serializes two reservations, but does not wait for the KEY SHARE
    lock the caller itself may hold on the row (for example, after creating a record
    that references it in the same transaction), which would never be released.
    """
    for attempt in range(RESERVE_ATTEMPTS):
        try:
            with record.pool.cursor() as cr:
                cr.execute(
                    SQL(
                        "SELECT %s FROM %s WHERE id = %s FOR NO KEY UPDATE",
                        SQL(", ").join(SQL.identifier(name) for name in FIELDS),
                        SQL.identifier(record._table),
                        record.id,
                    )
                )
                stored_key, stored_request = cr.fetchone() or (None, None)
                if not (stored_key and stored_request):
                    stored_key = stored_key or key
                    stored_request = request
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env[record._name].browse(record.id).write(
                        dict(zip(FIELDS, (stored_key, stored_request), strict=True))
                    )
            break
        except SerializationFailure:
            # the other transaction committed after this snapshot was taken: the
            # next one sees its key
            if attempt == RESERVE_ATTEMPTS - 1:
                raise
    record.invalidate_recordset(FIELDS)
    return stored_key, stored_request


def _write(record, values, committed):
    if not committed:
        record.sudo().write(values)
        return
    with record.pool.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        env[record._name].browse(record.id).write(values)
    record.invalidate_recordset(list(values))
