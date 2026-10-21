import base64
import logging
import time
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .edi_connector import (
    APPROVED_STATES,
    KUDE_MAX_WAIT,
    SifendeTransportError,
)

_logger = logging.getLogger(__name__)

CRON_BATCH_SIZE = 200
CRON_TIME_BUDGET = 45
# la cancelación vence a los 7 días de la emisión (OCA) y sobra un margen
CANCEL_WATCH_DAYS = 10
CANCEL_RECHECK_HOURS = 1
FAILED_RECHECK_HOURS = 1
PAYMENT_CHEQUE = "2"
DOC_INVOICE = "1"
DOC_CREDIT_NOTE = "5"
DOC_DEBIT_NOTE = "6"
DOC_DELIVERY_NOTE = "7"
CAPITAL_DEPARTMENT_CODE = 1
RESUBMITTED_STATES = ("PENDIENTE", "EN_LOTE")
WEBHOOK_STATES = {
    "documento.aprobado": "APROBADO",
    "documento.rechazado": "RECHAZADO",
    "documento.cancelado": "CANCELADO",
}


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_py_sifende_intento = fields.Integer(
        string="Intento de envío a Sifende",
        default=0,
        copy=False,
        readonly=True,
    )
    l10n_py_sifende_reintento_misma_clave = fields.Boolean(copy=False)
    l10n_py_sifende_envio_fallido = fields.Boolean(copy=False)
    l10n_py_sifende_archivos_pendientes = fields.Boolean(copy=False)
    l10n_py_sifende_ultima_consulta = fields.Datetime(copy=False, readonly=True)
    l10n_py_sifende_motivo_emision = fields.Selection(
        [
            ("1", "Devolución y ajuste de precios"),
            ("2", "Devolución"),
            ("3", "Descuento"),
            ("4", "Bonificación"),
            ("5", "Crédito incobrable"),
            ("6", "Recupero de costo"),
            ("7", "Recupero de gasto"),
            ("8", "Ajuste de precio"),
        ],
        string="Motivo de emisión (SIFEN)",
        compute="_compute_l10n_py_sifende_motivo_emision",
        store=True,
        readonly=False,
        copy=False,
    )
    l10n_py_sifende_responsable_emision = fields.Selection(
        [
            ("1", "Emisor de la factura"),
            ("2", "Poseedor de la factura y bienes"),
            ("3", "Empresa transportista"),
            ("4", "Despachante de aduanas"),
            ("5", "Agente de transporte o intermediario"),
        ],
        string="Responsable de la emisión de la NRE",
        default="1",
    )
    l10n_py_sifende_km_recorrido = fields.Integer(
        string="Kilómetros del recorrido (NRE)"
    )
    l10n_py_sifende_cheque_numero = fields.Char(
        string="Número del cheque (SIFEN)", size=8, copy=False
    )
    l10n_py_sifende_cheque_banco = fields.Char(
        string="Banco del cheque (SIFEN)", size=20, copy=False
    )

    @api.depends("reversed_entry_id", "l10n_latam_document_type_id")
    def _compute_l10n_py_sifende_motivo_emision(self):
        for move in self:
            code = move.l10n_latam_document_type_id.code
            if code == DOC_CREDIT_NOTE:
                move.l10n_py_sifende_motivo_emision = (
                    "2" if move.reversed_entry_id else "1"
                )
            elif code == DOC_DEBIT_NOTE:
                move.l10n_py_sifende_motivo_emision = "8"
            else:
                move.l10n_py_sifende_motivo_emision = False

    def _l10n_py_sifende_connector(self):
        self.ensure_one()
        return (
            self.env["l10n_py.edi.connector"]
            .sudo()
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("provider_type", "=", "sifende"),
                ],
                limit=1,
            )
        )

    def _l10n_py_sifende_idempotency_key(self):
        self.ensure_one()
        database = self.env["ir.config_parameter"].sudo().get_param("database.uuid")
        return f"odoo:{database}:{self.id}:{self.l10n_py_sifende_intento}"

    # === Datos adicionales para Sifende ===

    def _prepare_edi_document_data(self):
        data = super()._prepare_edi_document_data()
        if self._l10n_py_sifende_connector():
            data["sifende"] = self._l10n_py_sifende_extra_data()
        return data

    def _l10n_py_sifende_extra_data(self):
        self.ensure_one()
        code = self.l10n_latam_document_type_id.code
        extra = {
            "moveId": self.id,
            "idempotencyKey": self._l10n_py_sifende_idempotency_key(),
            "montoTotal": self.amount_total,
            "totalOperacion": self._l10n_py_sifende_operation_total(),
            "decimales": self.currency_id.decimal_places,
            "cliente": {
                "email": self.partner_id.email or None,
                "codigoCliente": self.partner_id.ref or None,
                "ciudad": self._l10n_py_sifende_city_name(),
            },
            "items": [
                self._l10n_py_sifende_item_data(line)
                for line in self.invoice_line_ids.filtered(
                    lambda line: line.display_type not in ("line_section", "line_note")
                )
            ],
        }
        if code in (DOC_CREDIT_NOTE, DOC_DEBIT_NOTE):
            extra["motivoEmision"] = (
                int(self.l10n_py_sifende_motivo_emision or 0) or None
            )
        if code == DOC_DELIVERY_NOTE:
            extra["remision"] = {
                "responsableEmision": int(
                    self.l10n_py_sifende_responsable_emision or 1
                ),
                "kilometrosRecorrido": self.l10n_py_sifende_km_recorrido,
            }
        return extra

    def _l10n_py_sifende_operation_total(self):
        # sin la línea de redondeo de efectivo: es lo que Sifende redondea
        self.ensure_one()
        return self.currency_id.round(
            sum(
                line.price_total
                for line in self.invoice_line_ids
                if line.display_type == "product"
            )
        )

    def _l10n_py_sifende_city_name(self):
        partner = self.partner_id
        # res.city de OCA no usa los códigos de SIFEN; Capital tiene una sola ciudad
        if partner.l10n_py_department_code == CAPITAL_DEPARTMENT_CODE:
            return "Asunción"
        return partner.city_id.name or partner.city

    @api.model
    def _l10n_py_sifende_item_data(self, line):
        uom = line.product_uom_id or line.product_id.uom_id
        untaxed_rate = sum(
            tax.amount
            for tax in line.tax_ids
            if tax.amount_type == "percent" and not tax.price_include
        )
        item = {
            "unidadMedida": uom.l10n_py_sifende_unidad_medida or 77,
            "precioUnitario": round(line.price_unit * (1 + untaxed_rate / 100), 8),
        }
        if line.discount:
            item["descuentoParticularPorcentaje"] = line.discount
        return item

    def _l10n_py_sifende_is_cash(self):
        self.ensure_one()
        due_dates = [due for due, _amount in self._l10n_py_sifende_installments()]
        return not due_dates or due_dates == [self.invoice_date]

    def _prepare_payment_condition(self):
        if not self._l10n_py_sifende_connector():
            return super()._prepare_payment_condition()
        self.ensure_one()
        if self._l10n_py_sifende_is_cash():
            payment = {
                "tipo": int(self.l10n_py_payment_type or "1"),
                "monto": str(self.amount_total),
                "moneda": self.currency_id.name,
                "cambio": 0,
            }
            if self.l10n_py_payment_type == PAYMENT_CHEQUE:
                payment["cheque"] = {
                    "numeroCheque": self.l10n_py_sifende_cheque_numero,
                    "bancoCheque": self.l10n_py_sifende_cheque_banco,
                }
            return {"tipo": 1, "entregas": [payment]}
        installments = self._l10n_py_sifende_installments()
        term_days = (installments[0][0] - self.invoice_date).days
        return {
            "tipo": 2,
            "credito": {
                "tipo": 1,
                "plazo": f"{term_days} días",
                "cuotas": len(installments),
                "infoCuotas": [
                    {
                        "moneda": self.currency_id.name,
                        "monto": amount,
                        "vencimiento": due.strftime("%Y-%m-%d"),
                    }
                    for due, amount in installments
                ],
            },
        }

    def _l10n_py_sifende_installments(self):
        """Vencimientos y montos que Odoo calculó para las cuentas por cobrar."""
        self.ensure_one()
        if not self.invoice_payment_term_id:
            return []
        amounts = {}
        for line in self.line_ids:
            if line.display_type == "payment_term" and line.date_maturity:
                amounts[line.date_maturity] = (
                    amounts.get(line.date_maturity, 0.0) + line.amount_currency
                )
        return [
            (due, self.currency_id.round(amount))
            for due, amount in sorted(amounts.items())
        ]

    def _validate_edi_document_type(self):
        errors = super()._validate_edi_document_type()
        if not self._l10n_py_sifende_connector():
            return errors
        code = self.l10n_latam_document_type_id.code
        if not self.invoice_date:
            errors.append(self.env._("Indique la fecha de la factura."))
        if not (self.journal_id.l10n_py_authorization_id.name or "").strip().isdigit():
            errors.append(
                self.env._("El número de timbrado debe ser numérico para Sifende.")
            )
        if (
            code == DOC_INVOICE
            and self.l10n_py_payment_type == PAYMENT_CHEQUE
            and self._l10n_py_sifende_is_cash()
        ):
            errors.extend(self._l10n_py_sifende_cheque_errors())
        if (
            code in (DOC_CREDIT_NOTE, DOC_DEBIT_NOTE)
            and not self.l10n_py_sifende_motivo_emision
        ):
            errors.append(self.env._("Indique el motivo de emisión de la nota."))
        if code == DOC_DELIVERY_NOTE:
            if self.l10n_py_sifende_km_recorrido < 1:
                errors.append(
                    self.env._(
                        "Nota de Remisión: indique los kilómetros del recorrido."
                    )
                )
            if not self.l10n_py_transport_id:
                errors.append(
                    self.env._(
                        "Nota de Remisión: los datos de transporte son obligatorios."
                    )
                )
        return errors

    def _l10n_py_sifende_cheque_errors(self):
        number = self.l10n_py_sifende_cheque_numero or ""
        bank = (self.l10n_py_sifende_cheque_banco or "").strip()
        errors = []
        if not (number.isascii() and number.isdigit()):
            errors.append(
                self.env._(
                    "Pago con cheque: indique el número del cheque (solo dígitos)."
                )
            )
        if not 4 <= len(bank) <= 20:
            errors.append(
                self.env._("Pago con cheque: indique el banco (de 4 a 20 caracteres).")
            )
        return errors

    # === Envío y respuesta ===

    def _l10n_py_sifende_check_posted(self):
        self.ensure_one()
        if self.state != "posted":
            raise UserError(
                self.env._(
                    "Confirme la factura antes de enviarla a SIFENDE: solo se emiten "
                    "documentos publicados."
                )
            )

    def action_send_edi(self):
        self.ensure_one()
        connector = self._l10n_py_sifende_connector()
        if not connector:
            return super().action_send_edi()
        self._l10n_py_sifende_check_posted()
        if self._l10n_py_sifende_is_failed():
            raise UserError(self._l10n_py_sifende_failed_steps())
        self._validate_edi_data()
        document_data = self._prepare_edi_document_data()
        self.l10n_py_edi_status = "sent"
        try:
            response = connector.send_document(document_data)
        except UserError as e:
            response = {"success": False, "edi_status": "error", "error": str(e)}
        if response.get("success"):
            self.l10n_py_sifende_reintento_misma_clave = False
            self._process_edi_response(response)
            return
        self.write(
            {
                "l10n_py_edi_status": response.get("edi_status") or "error",
                "l10n_py_edi_message": response.get("error"),
                "l10n_py_sifende_reintento_misma_clave": bool(
                    response.get("transient")
                ),
                "l10n_py_sifende_envio_fallido": False,
            }
        )

    def _process_edi_response(self, response):
        self.ensure_one()
        if not self._l10n_py_sifende_connector():
            return super()._process_edi_response(response)
        result = response.get("result") or {}
        cdc = result.get("cdc")
        self.write(
            {
                "l10n_py_cdc": cdc,
                "l10n_py_qr_string": result.get("qr"),
                "l10n_py_edi_status": "processing",
                "l10n_py_sifende_envio_fallido": False,
                "l10n_py_edi_batch_id": cdc,
                "l10n_py_edi_message": self.env._(
                    "Enviado a Sifende; esperando la respuesta de SIFEN."
                ),
            }
        )
        self._l10n_py_generate_qr_image()

    def action_cancel_edi(self):
        self.ensure_one()
        if self._l10n_py_sifende_connector():
            self.check_access("write")
        return super().action_cancel_edi()

    def action_retry_edi(self):
        self.ensure_one()
        if self._l10n_py_sifende_connector():
            self._l10n_py_sifende_check_posted()
            if self.l10n_py_edi_status == "rejected" and self.l10n_py_cdc:
                raise UserError(self._l10n_py_sifende_rejection_steps())
            if self._l10n_py_sifende_is_failed():
                raise UserError(self._l10n_py_sifende_failed_steps())
            if (
                self.l10n_py_edi_status in ("error", "rejected")
                and not self.l10n_py_sifende_reintento_misma_clave
            ):
                self.l10n_py_sifende_intento += 1
        return super().action_retry_edi()

    def _l10n_py_sifende_is_failed(self):
        self.ensure_one()
        return self.l10n_py_edi_status == "error" and self.l10n_py_sifende_envio_fallido

    def _l10n_py_sifende_failed_steps(self):
        return self.env._(
            "El envío a SIFEN de este documento falló y Odoo no lo reemite: el "
            "número y el CDC ya están tomados en Sifende. Reintente el lote desde "
            "el panel de Sifende o pídalo a soporte. Odoo actualiza el estado solo "
            "cuando el documento vuelva a procesarse (mismo CDC)."
        )

    def _l10n_py_sifende_rejection_steps(self):
        return self.env._(
            "SIFEN rechazó este documento y su número no se puede reenviar. "
            "Inutilice el número desde Facturación Electrónica > Inutilización de "
            "números, pulse Restablecer a borrador y Cancelar en este documento y "
            "emita uno nuevo."
        )

    def _generate_kude(self):
        self.ensure_one()
        connector = self._l10n_py_sifende_connector()
        if not connector:
            return super()._generate_kude()
        if not self.l10n_py_cdc:
            return
        self.check_access("write")
        pdf = connector._sifende_fetch_kude(
            self.l10n_py_cdc,
            document=self,
            max_wait=self.env.context.get("l10n_py_sifende_kude_wait", KUDE_MAX_WAIT),
        )
        self.write(
            {
                "l10n_py_kude_pdf": base64.b64encode(pdf),
                "l10n_py_kude_filename": f"KUDE_{self.l10n_py_cdc}.pdf",
            }
        )

    # === Cron ===

    @api.model
    def _cron_sifende_check_status(self):
        deadline = time.monotonic() + CRON_TIME_BUDGET
        connectors = (
            self.env["l10n_py.edi.connector"]
            .sudo()
            .search([("provider_type", "=", "sifende")])
        )
        for connector in connectors:
            for move in self._l10n_py_sifende_to_sync(connector):
                if time.monotonic() > deadline:
                    return
                try:
                    with self.env.cr.savepoint():
                        move._l10n_py_sifende_sync(connector, kude_wait=0)
                except Exception:
                    _logger.exception(
                        "Error consultando el estado en Sifende del documento %s",
                        move.name,
                    )
                move.l10n_py_sifende_ultima_consulta = fields.Datetime.now()

    @api.model
    def _l10n_py_sifende_to_sync(self, connector):
        now = fields.Datetime.now()
        recheck = now - timedelta(hours=CANCEL_RECHECK_HOURS)
        failed_recheck = now - timedelta(hours=FAILED_RECHECK_HOURS)
        watch_from = fields.Date.context_today(self) - timedelta(days=CANCEL_WATCH_DAYS)
        # primero los que nunca se consultaron o hace más que se consultaron: así el
        # límite del lote rota por todos en lugar de repetir siempre los mismos
        return self.sudo().search(
            [
                ("company_id", "=", connector.company_id.id),
                ("l10n_py_cdc", "!=", False),
                "|",
                "|",
                "|",
                ("l10n_py_edi_status", "=", "processing"),
                ("l10n_py_sifende_archivos_pendientes", "=", True),
                "&",
                "&",
                ("l10n_py_edi_status", "=", "accepted"),
                ("invoice_date", ">=", watch_from),
                "|",
                ("l10n_py_sifende_ultima_consulta", "=", False),
                ("l10n_py_sifende_ultima_consulta", "<", recheck),
                "&",
                "&",
                ("l10n_py_edi_status", "=", "error"),
                ("l10n_py_sifende_envio_fallido", "=", True),
                "|",
                ("l10n_py_sifende_ultima_consulta", "=", False),
                ("l10n_py_sifende_ultima_consulta", "<", failed_recheck),
            ],
            limit=CRON_BATCH_SIZE,
            order="l10n_py_sifende_ultima_consulta asc nulls first, id",
        )

    def _l10n_py_sifende_sync(self, connector, kude_wait=KUDE_MAX_WAIT):
        self.ensure_one()
        if (
            self.l10n_py_edi_status in ("processing", "accepted")
            or self._l10n_py_sifende_is_failed()
        ):
            self._l10n_py_sifende_refresh_state(connector)
        if self.l10n_py_edi_status == "accepted":
            self._l10n_py_sifende_fetch_files(connector, kude_wait)

    def _l10n_py_sifende_refresh_state(self, connector, announced=None, message=None):
        self.ensure_one()
        result = connector._sifende_check_status(self.l10n_py_cdc).get("result") or {}
        state = result.get("estado")
        if not (announced and message and self._sifende_same_outcome(announced, state)):
            message = result.get("mensajeRechazo")
        self._l10n_py_sifende_apply_state(state, message)

    @staticmethod
    def _sifende_same_outcome(announced, state):
        if announced == "APROBADO":
            return state in APPROVED_STATES
        return state == announced

    def _l10n_py_sifende_apply_state(self, state, message=None):
        self.ensure_one()
        status = self.l10n_py_edi_status
        failed = self._l10n_py_sifende_is_failed()
        in_flight = status == "processing" or failed
        if state in APPROVED_STATES and in_flight:
            self.write(
                {
                    "l10n_py_edi_status": "accepted",
                    "l10n_py_sifende_envio_fallido": False,
                    "l10n_py_edi_message": message
                    or self.env._("Documento aprobado por SIFEN."),
                }
            )
        elif state == "RECHAZADO" and in_flight:
            self.write(
                {
                    "l10n_py_edi_status": "rejected",
                    "l10n_py_edi_message": "\n\n".join(
                        (
                            message or self.env._("SIFEN rechazó el documento."),
                            self._l10n_py_sifende_rejection_steps(),
                        )
                    ),
                    "l10n_py_sifende_reintento_misma_clave": False,
                    "l10n_py_sifende_envio_fallido": False,
                }
            )
        elif state == "ERROR" and status == "processing":
            self.write(
                {
                    "l10n_py_edi_status": "error",
                    "l10n_py_sifende_envio_fallido": True,
                    "l10n_py_sifende_reintento_misma_clave": False,
                    "l10n_py_edi_message": "\n\n".join(
                        filter(None, (message, self._l10n_py_sifende_failed_steps()))
                    ),
                }
            )
        elif state in RESUBMITTED_STATES and failed:
            self.write(
                {
                    "l10n_py_edi_status": "processing",
                    "l10n_py_sifende_envio_fallido": False,
                    "l10n_py_edi_message": self.env._(
                        "Sifende reintentó el envío; esperando la respuesta de SIFEN."
                    ),
                }
            )
        elif state == "CANCELADO" and status in ("processing", "accepted"):
            self.write({"l10n_py_edi_status": "cancelled"})

    def _l10n_py_sifende_apply_webhook(self, connector, event_type, data):
        self.ensure_one()
        announced = WEBHOOK_STATES[event_type]
        status = self.l10n_py_edi_status
        # los avisos pueden llegar desordenados: el estado vigente lo dice la consulta
        if (
            status == "processing"
            or self._l10n_py_sifende_is_failed()
            or (announced == "CANCELADO" and status == "accepted")
        ):
            results = data.get("resultados")
            message = "\n".join(
                f"{result.get('codigo')}: {result['mensaje']}"
                for result in results or []
                if isinstance(result, dict) and result.get("mensaje")
            )
            self._l10n_py_sifende_refresh_state(connector, announced, message or None)
        if self.l10n_py_edi_status == "accepted":
            self._l10n_py_sifende_fetch_files(connector, kude_wait=0)

    def _l10n_py_sifende_fetch_files(self, connector, kude_wait=KUDE_MAX_WAIT):
        self.ensure_one()
        pending = False
        if not self.l10n_py_edi_xml:
            try:
                xml = connector._sifende_fetch_xml(self.l10n_py_cdc, document=self)
                self.write(
                    {
                        "l10n_py_edi_xml": base64.b64encode(xml),
                        "l10n_py_edi_xml_filename": f"{self.l10n_py_cdc}.xml",
                    }
                )
            except (UserError, SifendeTransportError) as e:
                pending = True
                _logger.info("XML de %s pendiente: %s", self.l10n_py_cdc, e)
        if not self.l10n_py_kude_pdf:
            try:
                self.with_context(l10n_py_sifende_kude_wait=kude_wait)._generate_kude()
            except (UserError, SifendeTransportError) as e:
                pending = True
                _logger.info("KuDE de %s pendiente: %s", self.l10n_py_cdc, e)
        self.l10n_py_sifende_archivos_pendientes = pending
