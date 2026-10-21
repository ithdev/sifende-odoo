import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests
from requests.structures import CaseInsensitiveDict

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.request_builder import SifendeRequestBuilder

_logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.sifende.com.py"
API_PREFIX = "/api/v1"
PANEL_BILLING_URL = "https://app.sifende.com.py"
USER_AGENT = "odoo-l10n_py_edi_sifende/18.0"

ENUM_CACHE_TTL = 24 * 3600
_ENUM_CACHE = {}

RETRY_BACKOFF = (1, 2)
RETRY_AFTER_CAP = 10
KUDE_MAX_WAIT = 30
KUDE_DEFAULT_POLL = 5
TRANSIENT_STATUS = (429, 500, 502, 503, 504)
TRANSIENT_SLUGS = ("idempotency-in-progress", "idempotency-upstream-unknown")

APPROVED_STATES = ("APROBADO", "APROBADO_OBSERVACION")
PENDING_STATES = ("PENDIENTE", "EN_LOTE")

WEBHOOK_PATH = "/l10n_py_edi_sifende/webhook"
WEBHOOK_EVENTS = ("documento.aprobado", "documento.rechazado", "documento.cancelado")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")

GROUP_READONLY = "account.group_account_readonly"
GROUP_BILLING = "account.group_account_invoice"
GROUP_MANAGER = "account.group_account_manager"
READ_GROUPS = (GROUP_READONLY, GROUP_BILLING)


class SifendeTransportError(Exception):
    """Timeout o conexión caída: la solicitud pudo no haber llegado."""


@dataclass
class SifendeResponse:
    status_code: int
    headers: dict = field(default_factory=CaseInsensitiveDict)
    body: object = None
    content: bytes = b""

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    @property
    def slug(self):
        problem_type = self.body.get("type") if isinstance(self.body, dict) else None
        return problem_type.rstrip("/").rsplit("/", 1)[-1] if problem_type else None

    @property
    def detail(self):
        if not isinstance(self.body, dict):
            return None
        return (
            self.body.get("detail") or self.body.get("title") or self.body.get("error")
        )

    @property
    def field_errors(self):
        errors = self.body.get("errores") if isinstance(self.body, dict) else None
        return errors if isinstance(errors, dict) else {}

    @property
    def retry_after(self):
        try:
            return int(self.headers.get("Retry-After"))
        except (TypeError, ValueError):
            return None


def is_secure_url(url):
    parts = urlsplit(url or "")
    host = parts.hostname or ""
    if not host:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS)


def mask_key(api_key):
    if not api_key:
        return ""
    parts = api_key.split("_")
    prefix = "_".join(parts[:2]) + "_" if len(parts) > 2 else ""
    return f"{prefix}****"


class EDIConnector(models.Model):
    _inherit = "l10n_py.edi.connector"

    provider_type = fields.Selection(
        selection_add=[("sifende", "Sifende")],
        ondelete={"sifende": "cascade"},
    )
    sifende_api_key = fields.Char(
        string="API key de Sifende",
        groups="base.group_system",
        copy=False,
    )
    sifende_base_url = fields.Char(
        string="URL de Sifende",
        default=DEFAULT_BASE_URL,
        groups="base.group_system",
    )
    sifende_ambiente = fields.Char(
        string="Ambiente de la API key",
        readonly=True,
        copy=False,
    )

    sifende_webhook_url = fields.Char(
        string="URL del webhook",
        compute="_compute_sifende_webhook_url",
    )
    sifende_webhook_id = fields.Integer(
        string="Webhook en Sifende",
        groups="base.group_system",
        readonly=True,
        copy=False,
    )
    sifende_webhook_secret = fields.Char(
        string="Secreto del webhook",
        groups="base.group_system",
        copy=False,
    )

    @api.depends("provider_type")
    def _compute_sifende_webhook_url(self):
        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        for connector in self:
            connector.sifende_webhook_url = (
                f"{base_url}{WEBHOOK_PATH}/{connector.id}"
                if connector.id and connector.provider_type == "sifende"
                else False
            )

    @api.constrains("sifende_base_url")
    def _check_sifende_base_url(self):
        for connector in self.sudo():
            if connector.sifende_base_url and not is_secure_url(
                connector.sifende_base_url
            ):
                raise ValidationError(self._sifende_insecure_url_message())

    def _sifende_insecure_url_message(self):
        return self.env._(
            "La URL de Sifende debe usar HTTPS; HTTP solo se admite para localhost."
        )

    def write(self, vals):
        if "sifende_api_key" in vals and "sifende_ambiente" not in vals:
            vals = {**vals, "sifende_ambiente": False}
        return super().write(vals)

    # === Interfaz pública del conector ===

    def _sifende_check_access(self, *groups):
        self.ensure_one()
        # OCA llama con sudo(): se valida al usuario real, no al superusuario
        connector = self.sudo(False)
        if connector.env.su:
            return
        connector.check_access("read")
        if not any(connector.env.user.has_group(group) for group in groups):
            raise AccessError(
                self.env._("No tiene permisos para operar el conector de Sifende.")
            )

    @api.private
    def send_document(self, invoice_data):
        if self.provider_type != "sifende":
            return super().send_document(invoice_data)
        self._sifende_check_access(GROUP_BILLING)
        return self._sifende_send_document(invoice_data)

    @api.private
    def check_status(self, document_id):
        if self.provider_type != "sifende":
            return super().check_status(document_id)
        self._sifende_check_access(*READ_GROUPS)
        return self._sifende_check_status(document_id)

    @api.private
    def cancel_document(self, document_id, reason=""):
        if self.provider_type != "sifende":
            return super().cancel_document(document_id, reason)
        self._sifende_check_access(GROUP_BILLING)
        return self._sifende_cancel_document(document_id, reason)

    @api.private
    def inutilize_range(self, data):
        if self.provider_type != "sifende":
            return super().inutilize_range(data)
        self._sifende_check_access(GROUP_MANAGER)
        return self._sifende_inutilize_range(data)

    @api.private
    def preview_document(self, invoice_data):
        if self.provider_type != "sifende":
            return super().preview_document(invoice_data)
        raise UserError(self._sifende_no_preview_message())

    @api.private
    def preview_qr(self, invoice_data):
        if self.provider_type != "sifende":
            return super().preview_qr(invoice_data)
        raise UserError(self._sifende_no_preview_message())

    def test_connection(self):
        if self.provider_type != "sifende":
            return super().test_connection()
        self._sifende_check_access(GROUP_MANAGER)
        return self._sifende_test_connection()

    def _sifende_fetch_xml(self, cdc, document=None):
        self.ensure_one()
        response = self._sifende_call(
            "GET",
            f"/documento-electronico/{cdc}/xml",
            operation="download_xml",
            document=document,
            cdc=cdc,
            binary=True,
        )
        if response.ok and response.content:
            return response.content
        if response.status_code == 409:
            raise UserError(
                self.env._("SIFENDE todavía no tiene el XML de este documento.")
            )
        raise UserError(self._sifende_failure_message(response))

    def _sifende_fetch_kude(self, cdc, document=None, max_wait=KUDE_MAX_WAIT):
        self.ensure_one()
        params = None
        waited = 0
        while True:
            response = self._sifende_call(
                "GET",
                f"/documento-electronico/{cdc}/kude",
                operation="download_pdf",
                document=document,
                cdc=cdc,
                params=params,
                binary=True,
            )
            if response.status_code == 200 and response.content:
                return response.content
            if response.status_code != 202:
                raise UserError(self._sifende_failure_message(response))
            wait = max(
                1, min(response.retry_after or KUDE_DEFAULT_POLL, RETRY_AFTER_CAP)
            )
            if waited + wait > max_wait:
                raise UserError(
                    self.env._(
                        "SIFENDE sigue preparando el KuDE; se reintentará más tarde."
                    )
                )
            time.sleep(wait)
            waited += wait
            params = {"soloConsulta": "true"}

    # === Operaciones ===

    def _sifende_send_document(self, invoice_data):
        self.ensure_one()
        extra = invoice_data.get("sifende") or {}
        payload = SifendeRequestBuilder(self.env, self._sifende_enum_name).build(
            invoice_data
        )
        key = extra.get("idempotencyKey")
        document = (
            self.env["account.move"].browse(extra["moveId"])
            if extra.get("moveId")
            else None
        )
        try:
            response = self._sifende_call(
                "POST",
                "/documento-electronico",
                operation="send",
                json_body=payload,
                idempotency_key=key,
                retries=len(RETRY_BACKOFF) if key else 0,
                document=document,
            )
        except SifendeTransportError as e:
            return {
                "success": False,
                "edi_status": "error",
                "transient": True,
                "error": self.env._(
                    "No se pudo contactar a SIFENDE (%(error)s). Al reintentar se "
                    "usará la misma clave de idempotencia: no se duplica el documento.",
                    error=e,
                ),
            }
        if response.status_code in (200, 201, 202) and isinstance(response.body, dict):
            body = response.body
            return {
                "success": True,
                "result": {
                    "cdc": body.get("cdc"),
                    "qr": body.get("qrUrl"),
                    "numero": body.get("numeroFormateado")
                    or body.get("numeroDocumento"),
                    "estado": body.get("estado") or "PENDIENTE",
                },
            }
        return self._sifende_send_failure(response)

    def _sifende_send_failure(self, response):
        slug = response.slug
        failure = {"success": False, "edi_status": "error", "transient": False}
        if slug in ("document-number-in-use", "document-number-voided"):
            failure["error"] = self._sifende_number_conflict_message(response)
        elif slug in TRANSIENT_SLUGS or response.status_code in TRANSIENT_STATUS:
            failure["transient"] = True
            failure["error"] = self.env._(
                "SIFENDE no pudo procesar el envío ahora (HTTP %(status)s). "
                "Reintente: se usará la misma clave de idempotencia, "
                "sin duplicar el documento.",
                status=response.status_code,
            )
        elif slug == "timbrado-mismatch":
            failure["error"] = self.env._(
                "El timbrado de Odoo no coincide con el de SIFENDE. %(detail)s",
                detail=response.detail or "",
            )
        elif slug == "document-number-requires-voiding":
            failure["edi_status"] = "rejected"
            failure["error"] = self.env._(
                "El número de este documento ya pertenece a uno rechazado por SIFEN "
                "y no se puede reenviar. Inutilice el número desde Facturación "
                "Electrónica > Inutilización de números, cancele este documento "
                "(Restablecer a borrador y Cancelar) y emita uno nuevo. %(detail)s",
                detail=response.detail or "",
            )
        elif slug in (
            "document-quota-exceeded",
            "emission-suspended",
            "plan-operation-not-allowed",
        ):
            failure["error"] = self.env._(
                "Su plan de SIFENDE no permite emitir ahora: %(detail)s Revise la "
                "facturación en %(url)s.",
                detail=response.detail or "",
                url=PANEL_BILLING_URL,
            )
        elif response.status_code in (400, 422):
            failure["edi_status"] = "rejected"
            failure["error"] = self._sifende_failure_message(response)
        else:
            failure["error"] = self._sifende_failure_message(response)
        return failure

    def _sifende_number_conflict_message(self, response):
        body = response.body if isinstance(response.body, dict) else {}
        cdc = body.get("cdc") or body.get("cdcExistente")
        if response.slug == "document-number-voided":
            return self.env._(
                "El número está inutilizado en SIFENDE y no se puede usar. %(detail)s",
                detail=response.detail or "",
            )
        message = self.env._("El número de documento ya está en uso en SIFENDE.")
        if cdc:
            message += " " + self.env._("CDC existente: %(cdc)s.", cdc=cdc)
        return message

    def _sifende_check_status(self, cdc):
        self.ensure_one()
        try:
            response = self._sifende_call(
                "GET",
                f"/documento-electronico/status/{cdc}",
                operation="status",
                cdc=cdc,
            )
        except SifendeTransportError as e:
            return {"success": False, "error": str(e)}
        if not (response.ok and isinstance(response.body, dict)):
            return {"success": False, "error": self._sifende_failure_message(response)}
        body = response.body
        estado = body.get("estado")
        result = dict(body)
        message = body.get("mensajeRechazo")
        if estado in APPROVED_STATES:
            result["status"] = "Aprobado"
            return {"success": True, "result": result}
        if estado == "RECHAZADO":
            result["status"] = "Rechazado"
            return {"success": False, "result": result, "error": message or estado}
        return {"success": False, "result": result, "error": message or estado}

    def _sifende_cancel_document(self, cdc, reason):
        self.ensure_one()
        reason = (reason or self.env.context.get("l10n_py_cancel_motive") or "").strip()
        if len(reason) < 5:
            reason = self.env._("Cancelación solicitada desde Odoo")
        return self._sifende_event(
            "cancel",
            f"/documento-electronico/{cdc}/cancelar",
            {"motivo": reason[:500]},
            cdc,
        )

    def _sifende_inutilize_range(self, data):
        self.ensure_one()
        try:
            timbrado = int(data["timbrado"])
        except (TypeError, ValueError) as e:
            raise UserError(self.env._("El timbrado debe ser numérico.")) from e
        payload = {
            "numeroTimbrado": timbrado,
            "establecimiento": data["establecimiento"],
            "puntoExpedicion": data["punto"],
            "numeroInicio": data["numeroDesde"],
            "numeroFin": data["numeroHasta"],
            "tipoDocumento": self._sifende_inutilization_document_type(data),
            "motivo": (data.get("motivo") or "").strip()[:500],
        }
        return self._sifende_event(
            "event", "/documento-electronico/inutilizar", payload
        )

    def _sifende_inutilization_document_type(self, data):
        inutilization = self.env["l10n_py.number.inutilization"].browse(
            self.env.context.get("l10n_py_inutilization_id")
        )
        code = inutilization.authorization_id.l10n_latam_document_type_id.code
        return int(code or data["tipoDocumento"])

    def _sifende_event(self, operation, path, payload, cdc=None):
        try:
            response = self._sifende_call(
                "POST",
                path,
                operation=operation,
                json_body=payload,
                idempotency_key=str(uuid.uuid4()),
                retries=len(RETRY_BACKOFF),
                cdc=cdc,
            )
        except SifendeTransportError as e:
            return {"success": False, "error": str(e)}
        body = response.body if isinstance(response.body, dict) else {}
        if response.ok and body.get("estadoEvento") != "RECHAZADO":
            return {"success": True, "result": body}
        if response.ok:
            return {
                "success": False,
                "result": body,
                "error": body.get("mensajeRespuesta")
                or self.env._("SIFEN rechazó el evento."),
            }
        return {"success": False, "error": self._sifende_failure_message(response)}

    def _sifende_test_connection(self):
        self.ensure_one()
        try:
            response = self._sifende_call("GET", "/contribuyente", operation="validate")
        except SifendeTransportError as e:
            raise UserError(
                self.env._("No se pudo contactar a SIFENDE: %(error)s", error=e)
            ) from e
        if not (response.ok and isinstance(response.body, dict)):
            raise UserError(self._sifende_failure_message(response))
        taxpayer = response.body
        environment = taxpayer.get("ambiente")
        self.sudo().sifende_ambiente = environment
        expected = ("PROD",) if self.environment == "prod" else ("DEV", "SANDBOX")
        if environment not in expected:
            raise UserError(
                self.env._(
                    "El conector está en ambiente «%(local)s» pero la API key es del "
                    "ambiente %(remote)s de SIFENDE.",
                    local=dict(self._fields["environment"].selection)[self.environment],
                    remote=environment,
                )
            )
        ready = (taxpayer.get("estadoConfiguracion") or {}).get("listoParaEmitir")
        warnings = self._sifende_config_warnings(taxpayer)
        lines = [
            self.env._("Razón social: %(name)s", name=taxpayer.get("razonSocial")),
            self.env._("Ambiente: %(env)s", env=environment),
            self.env._(
                "Listo para emitir: %(ready)s",
                ready=self.env._("Sí") if ready else self.env._("No"),
            ),
            *warnings,
        ]
        ok = ready and not warnings
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Conexión con SIFENDE")
                if ok
                else self.env._("Conexión con SIFENDE: revise la configuración"),
                "message": " ".join(
                    line if line.endswith(".") else f"{line}." for line in lines
                ),
                "type": "success" if ok else "warning",
                "sticky": not ok,
            },
        }

    def _sifende_config_warnings(self, taxpayer):
        warnings = []
        # el timbrado de Sandbox es sintético: no se compara con el de Odoo
        remote_stamp = (
            None
            if taxpayer.get("ambiente") == "SANDBOX"
            else (taxpayer.get("timbrado") or {}).get("numero")
        )
        points = {
            (est["numeroEstablecimiento"], point["puntoExpedicion"])
            for est in taxpayer.get("establecimientos") or []
            for point in est.get("puntosExpedicion") or []
        }
        authorizations = (
            self.env["account.authorization"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("date_to", ">=", fields.Date.context_today(self)),
                ]
            )
        )
        for auth in authorizations:
            if (
                remote_stamp is not None
                and str(remote_stamp) != (auth.name or "").strip()
            ):
                warnings.append(
                    self.env._(
                        "El timbrado %(odoo)s de Odoo no coincide con el de SIFENDE "
                        "(%(remote)s).",
                        odoo=auth.name,
                        remote=remote_stamp,
                    )
                )
            try:
                key = (int(auth.establishment), int(auth.expedition_point))
            except (TypeError, ValueError):
                continue
            if points and key not in points:
                warnings.append(
                    self.env._(
                        "El establecimiento %(est)s / punto %(point)s del timbrado "
                        "%(stamp)s "
                        "no existe en SIFENDE.",
                        est=auth.establishment,
                        point=auth.expedition_point,
                        stamp=auth.name,
                    )
                )
        return list(dict.fromkeys(warnings))

    def _sifende_no_preview_message(self):
        return self.env._(
            "SIFENDE no genera una vista previa sin emitir el documento. "
            "Use «Enviar a EDI» para emitirlo."
        )

    # === Webhooks ===

    def action_sifende_register_webhook(self):
        self.ensure_one()
        self._sifende_check_webhook_provider()
        url = self._sifende_public_webhook_url()
        existing = self._sifende_find_webhook(url)
        if existing:
            self.sudo().sifende_webhook_id = existing["id"]
            if self.sudo().sifende_webhook_secret:
                message = self.env._("El webhook ya está registrado en Sifende.")
            else:
                message = self.env._(
                    "La URL ya estaba registrada en Sifende, pero el secreto solo se "
                    "muestra al crearlo. Use «Rotar secreto» para obtener uno nuevo."
                )
            return self._sifende_notify(message, "warning")
        response = self._sifende_webhook_call(
            "POST", "", json_body={"url": url, "eventos": list(WEBHOOK_EVENTS)}
        )
        if not (response.ok and isinstance(response.body, dict)):
            raise UserError(self._sifende_failure_message(response))
        self._sifende_store_webhook(response.body)
        return self._sifende_notify(
            self.env._("Webhook registrado en Sifende: %(url)s", url=url), "success"
        )

    def action_sifende_rotate_webhook_secret(self):
        self.ensure_one()
        self._sifende_check_webhook_provider()
        webhook_id = self._sifende_require_webhook_id()
        response = self._sifende_webhook_call("POST", f"/{webhook_id}/rotar")
        if response.status_code == 404:
            # sin excepción: el rollback del RPC deshacería la limpieza
            self._sifende_forget_webhook()
            return self._sifende_notify(
                self._sifende_webhook_missing_message(), "warning"
            )
        if not (response.ok and isinstance(response.body, dict)):
            raise UserError(self._sifende_failure_message(response))
        self._sifende_store_webhook(response.body)
        return self._sifende_notify(
            self.env._("Secreto del webhook rotado y guardado."), "success"
        )

    def action_sifende_remove_webhook(self):
        self.ensure_one()
        self._sifende_check_webhook_provider()
        webhook_id = self._sifende_require_webhook_id()
        response = self._sifende_webhook_call("DELETE", f"/{webhook_id}")
        if not (response.ok or response.status_code == 404):
            raise UserError(self._sifende_failure_message(response))
        self._sifende_forget_webhook()
        return self._sifende_notify(
            self.env._("Webhook eliminado de Sifende."), "success"
        )

    def _sifende_check_webhook_provider(self):
        if self.provider_type != "sifende":
            raise UserError(self.env._("El conector no es de Sifende."))
        if not self.env.is_system():
            raise UserError(
                self.env._("Solo un administrador puede gestionar el webhook.")
            )

    def _sifende_public_webhook_url(self):
        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        parts = urlsplit(base_url)
        if parts.scheme != "https" or (parts.hostname or "") in LOCAL_HOSTS:
            raise UserError(
                self.env._(
                    "Sifende exige una URL pública con HTTPS y la URL base de Odoo "
                    "(web.base.url) es «%(url)s». Configúrela en Ajustes > Técnico > "
                    "Parámetros del sistema.",
                    url=base_url,
                )
            )
        return f"{base_url}{WEBHOOK_PATH}/{self.id}"

    def _sifende_find_webhook(self, url):
        response = self._sifende_webhook_call("GET", "")
        if not (response.ok and isinstance(response.body, list)):
            raise UserError(self._sifende_failure_message(response))
        for webhook in response.body:
            if (webhook.get("url") or "").rstrip("/") == url:
                return webhook
        return None

    def _sifende_webhook_call(self, method, suffix, json_body=None):
        try:
            return self._sifende_call(
                method,
                f"/contribuyente/webhooks{suffix}",
                operation="event",
                json_body=json_body,
            )
        except SifendeTransportError as e:
            raise UserError(
                self.env._("No se pudo contactar a SIFENDE: %(error)s", error=e)
            ) from e

    def _sifende_store_webhook(self, body):
        self.sudo().write(
            {
                "sifende_webhook_id": (body.get("endpoint") or {}).get("id"),
                "sifende_webhook_secret": body.get("secreto"),
            }
        )

    def _sifende_forget_webhook(self):
        self.sudo().write({"sifende_webhook_id": 0, "sifende_webhook_secret": False})

    def _sifende_require_webhook_id(self):
        webhook_id = self.sudo().sifende_webhook_id
        if not webhook_id:
            raise UserError(self.env._("El conector no tiene un webhook registrado."))
        return webhook_id

    def _sifende_webhook_missing_message(self):
        return self.env._("El webhook ya no existe en Sifende. Regístrelo de nuevo.")

    def _sifende_notify(self, message, notification_type):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Webhook de Sifende"),
                "message": message,
                "type": notification_type,
                "sticky": notification_type != "success",
            },
        }

    # === Catálogo de enumeraciones ===

    def _sifende_enums(self):
        key = self._sifende_base_url()
        cached = _ENUM_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            response = self._sifende_call(
                "GET",
                "/public/enums",
                operation="validate",
                authenticated=False,
                log=False,
            )
        except SifendeTransportError as e:
            raise UserError(
                self.env._(
                    "No se pudo obtener el catálogo de SIFENDE: %(error)s", error=e
                )
            ) from e
        if not (response.ok and isinstance(response.body, dict)):
            raise UserError(self._sifende_failure_message(response))
        _ENUM_CACHE[key] = (time.monotonic() + ENUM_CACHE_TTL, response.body)
        return response.body

    def _sifende_enum_name(self, category, code):
        for value in self._sifende_enums().get(category) or []:
            if value.get("val") is not None and int(value["val"]) == int(code):
                return value["name"]
        return None

    @api.model
    def _sifende_clear_enum_cache(self):
        _ENUM_CACHE.clear()

    # === HTTP ===

    def _sifende_base_url(self):
        return (self.sudo().sifende_base_url or DEFAULT_BASE_URL).rstrip("/")

    def _sifende_call(
        self,
        method,
        path,
        *,
        operation,
        json_body=None,
        params=None,
        idempotency_key=None,
        retries=0,
        document=None,
        cdc=None,
        binary=False,
        authenticated=True,
        log=True,
    ):
        self.ensure_one()
        api_key = self.sudo().sifende_api_key
        if authenticated and not api_key:
            raise UserError(
                self.env._(
                    "Configure la API key de Sifende en el conector EDI de la empresa."
                )
            )
        url = f"{self._sifende_base_url()}{API_PREFIX}{path}"
        if authenticated and not is_secure_url(url):
            raise UserError(self._sifende_insecure_url_message())
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if authenticated:
            headers["Authorization"] = f"Bearer {api_key}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if binary:
            headers["Accept"] = "*/*"

        started = time.monotonic()
        attempt = 0
        transport_error = None
        response = None
        while True:
            transport_error = response = None
            try:
                raw = requests.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout or 30,
                )
                response = self._sifende_parse(raw, binary)
            except (requests.Timeout, requests.ConnectionError) as e:
                transport_error = e
            if attempt >= retries or not self._sifende_should_retry(
                response, transport_error
            ):
                break
            time.sleep(self._sifende_backoff(attempt, response))
            attempt += 1

        if log:
            self._sifende_log(
                operation,
                method,
                url,
                json_body,
                response,
                transport_error,
                started,
                attempt,
                document,
                cdc,
                headers,
            )
        if transport_error is not None:
            raise SifendeTransportError(
                self._sifende_transport_message(transport_error)
            )
        return response

    @staticmethod
    def _sifende_should_retry(response, transport_error):
        if transport_error is not None:
            return True
        return (
            response.status_code in TRANSIENT_STATUS or response.slug in TRANSIENT_SLUGS
        )

    @staticmethod
    def _sifende_backoff(attempt, response):
        if response is not None and response.retry_after:
            return min(response.retry_after, RETRY_AFTER_CAP)
        return RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]

    @staticmethod
    def _sifende_parse(raw, binary):
        headers = CaseInsensitiveDict(raw.headers or {})
        content = raw.content or b""
        body = None
        content_type = (headers.get("Content-Type") or "").lower()
        if "json" in content_type or not binary:
            try:
                body = raw.json()
            except ValueError:
                body = None
        return SifendeResponse(
            raw.status_code, headers, body, content if binary else b""
        )

    def _sifende_transport_message(self, error):
        if isinstance(error, requests.Timeout):
            return self.env._("tiempo de espera agotado")
        return self.env._("conexión rechazada o interrumpida")

    def _sifende_failure_message(self, response):
        if response.status_code == 401:
            return self.env._(
                "SIFENDE rechazó la API key (401). Verifique que sea válida y no "
                "esté vencida."
            )
        parts = [
            response.detail
            or self.env._("Error HTTP %(status)s", status=response.status_code)
        ]
        parts.extend(
            f"{name}: {message}" for name, message in response.field_errors.items()
        )
        return "\n".join(parts)

    @staticmethod
    def _sifende_redact(body):
        if isinstance(body, dict) and body.get("secreto"):
            return {**body, "secreto": "whsec_****"}
        return body

    @staticmethod
    def _sifende_body_cdc(response):
        body = response.body if response is not None else None
        return body.get("cdc") if isinstance(body, dict) else None

    def _sifende_log(
        self,
        operation,
        method,
        url,
        json_body,
        response,
        transport_error,
        started,
        attempt,
        document,
        cdc,
        headers,
    ):
        api_key = self.sudo().sifende_api_key
        logged_headers = {
            name: (f"Bearer {mask_key(api_key)}" if name == "Authorization" else value)
            for name, value in headers.items()
        }
        failed = transport_error is not None or (
            response is not None and not response.ok
        )
        if response is not None and response.body is not None:
            response_data = self._sifende_redact(response.body)
        elif response is not None and response.content:
            response_data = f"<{len(response.content)} bytes>"
        else:
            response_data = None
        error_message = None
        if failed:
            error_message = (
                self._sifende_transport_message(transport_error)
                if transport_error is not None
                else self._sifende_failure_message(response)
            )
        self.env["l10n_py.edi.log"].log_operation(
            operation_type=operation,
            provider="sifende",
            document=document or None,
            request_data=json_body,
            response_data=response_data,
            execution_time=(time.monotonic() - started) * 1000,
            success=not failed,
            error_message=error_message,
            endpoint=url,
            method=method,
            request_headers=json.dumps(logged_headers, ensure_ascii=False),
            status_code=response.status_code if response is not None else 0,
            retry_count=attempt,
            cdc=cdc
            or self._sifende_body_cdc(response)
            or (document.l10n_py_cdc if document else False),
        )
