import json
import uuid

from odoo import SUPERUSER_ID, api

FIELDS = ("l10n_py_sifende_event_key", "l10n_py_sifende_event_request")


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
    # otro cursor: el rollback del error que ve el usuario no puede perderla
    request = build_request()
    key = key or str(uuid.uuid4())
    _write(
        record,
        {
            "l10n_py_sifende_event_key": key,
            "l10n_py_sifende_event_request": json.dumps(request),
        },
        committed=True,
    )
    return key, request


def forget(record, committed):
    if record and record.sudo().l10n_py_sifende_event_key:
        _write(record, dict.fromkeys(FIELDS, False), committed)


def pending_request(record):
    stored = record.sudo().l10n_py_sifende_event_request if record else False
    return json.loads(stored) if stored else {}


def _write(record, values, committed):
    if not committed:
        record.sudo().write(values)
        return
    with record.pool.cursor() as cr:
        env = api.Environment(cr, SUPERUSER_ID, {})
        env[record._name].browse(record.id).write(values)
    record.invalidate_recordset(list(values))
