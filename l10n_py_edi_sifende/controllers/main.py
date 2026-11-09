import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import time

from odoo import http
from odoo.http import request

from ..models.edi_connector import WEBHOOK_PATH
from ..services.document_state import WEBHOOK_STATES

_logger = logging.getLogger(__name__)

TIMESTAMP_TOLERANCE = 300
SIGNATURE_SEPARATORS = re.compile(r"[\s,]+")


class SifendeWebhookController(http.Controller):
    @http.route(
        f"{WEBHOOK_PATH}/<int:connector_id>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def receive(self, connector_id):
        connector = request.env["l10n_py.edi.connector"].sudo().browse(connector_id)
        if (
            not connector.exists()
            or not connector.active
            or connector.provider_type != "sifende"
        ):
            return self._reply(404)
        body = request.httprequest.get_data()
        if not self._is_authentic(connector, body):
            return self._reply(401)
        try:
            return self._reply(self._process(connector, body))
        except Exception:
            request.env.cr.rollback()
            _logger.exception(
                "Error procesando el webhook de Sifende del conector %s", connector.id
            )
            return self._reply(500)

    @staticmethod
    def _reply(status):
        return request.make_response("", status=status)

    def _is_authentic(self, connector, body):
        secret = connector.sifende_webhook_secret
        headers = request.httprequest.headers
        event_id = headers.get("X-Sifende-Event-Id")
        delivery_id = headers.get("X-Sifende-Delivery-Id")
        timestamp = headers.get("X-Sifende-Timestamp")
        signature = headers.get("X-Sifende-Signature")
        if not (secret and event_id and delivery_id and timestamp and signature):
            return False
        try:
            sent_at = int(timestamp)
        except ValueError:
            return False
        if abs(time.time() - sent_at) > TIMESTAMP_TOLERANCE:
            return False
        message = f"{event_id}.{delivery_id}.{timestamp}.".encode() + body
        expected = hmac.new(secret.encode(), message, hashlib.sha256).digest()
        return any(
            hmac.compare_digest(received, expected)
            for received in self._signatures(signature)
        )

    @staticmethod
    def _signatures(header):
        for part in SIGNATURE_SEPARATORS.split(header.strip()):
            if not part.startswith("v1="):
                continue
            try:
                yield base64.b64decode(part[3:], validate=True)
            except binascii.Error:
                continue

    def _process(self, connector, body):
        try:
            payload = json.loads(body)
        except ValueError:
            return 400
        if not isinstance(payload, dict):
            return 400
        event_type = payload.get("tipo")
        data = payload.get("data")
        if event_type not in WEBHOOK_STATES or not isinstance(data, dict):
            return 200
        move = self._find_move(connector, data.get("cdc"))
        if not move:
            _logger.info(
                "Webhook de Sifende para un CDC desconocido (%s): %s",
                connector.company_id.name,
                data.get("cdc"),
            )
            return 200
        event_id = request.httprequest.headers["X-Sifende-Event-Id"]
        event = request.env["l10n_py.sifende.webhook.event"].register(
            connector, event_id, event_type
        )
        if event and not move._l10n_py_sifende_apply_webhook(
            connector, event_type, data
        ):
            # sin la consulta de estado el aviso no se pudo aplicar: solo se libera el
            # registro del evento para que la reentrega se procese. No hay rollback,
            # así que queda la traza de la consulta fallida en el log EDI.
            event.unlink()
            _logger.warning(
                "Webhook de Sifende %s sin consumir: falló la consulta de estado de %s",
                event_id,
                data.get("cdc"),
            )
            return 503
        return 200

    @staticmethod
    def _find_move(connector, cdc):
        if not cdc or not isinstance(cdc, str):
            return request.env["account.move"]
        return (
            request.env["account.move"]
            .sudo()
            .with_company(connector.company_id)
            .search(
                [
                    ("company_id", "=", connector.company_id.id),
                    ("l10n_py_cdc", "=", cdc),
                ],
                limit=1,
            )
        )
