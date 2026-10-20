import base64
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .edi_connector import APPROVED_STATES, PENDING_STATES

_logger = logging.getLogger(__name__)

CRON_BATCH_SIZE = 200
DOC_CREDIT_NOTE = "5"
DOC_DEBIT_NOTE = "6"
DOC_DELIVERY_NOTE = "7"
CAPITAL_DEPARTMENT_CODE = 1


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_py_sifende_intento = fields.Integer(
        string="Intento de envío a Sifende",
        default=0,
        copy=False,
        readonly=True,
    )
    l10n_py_sifende_reintento_misma_clave = fields.Boolean(copy=False)
    l10n_py_sifende_archivos_pendientes = fields.Boolean(copy=False)
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

    def _prepare_payment_condition(self):
        if not self._l10n_py_sifende_connector():
            return super()._prepare_payment_condition()
        self.ensure_one()
        due_dates = self._l10n_py_sifende_due_dates()
        if not due_dates or due_dates == [self.invoice_date]:
            return {
                "tipo": 1,
                "entregas": [
                    {
                        "tipo": int(self.l10n_py_payment_type or "1"),
                        "monto": str(self.amount_total),
                        "moneda": self.currency_id.name,
                        "cambio": 0,
                    }
                ],
            }
        term_days = (due_dates[0] - self.invoice_date).days
        return {
            "tipo": 2,
            "credito": {
                "tipo": 1,
                "plazo": f"{term_days} días",
                "cuotas": len(due_dates),
                "infoCuotas": [
                    {
                        "moneda": self.currency_id.name,
                        "monto": self.amount_total / len(due_dates),
                        "vencimiento": due.strftime("%Y-%m-%d"),
                    }
                    for due in due_dates
                ],
            },
        }

    def _l10n_py_sifende_due_dates(self):
        self.ensure_one()
        if not self.invoice_payment_term_id:
            return []
        lines = self.line_ids.filtered(lambda line: line.display_type == "payment_term")
        return sorted({line.date_maturity for line in lines if line.date_maturity})

    def _validate_edi_document_type(self):
        errors = super()._validate_edi_document_type()
        if not self._l10n_py_sifende_connector():
            return errors
        code = self.l10n_latam_document_type_id.code
        if not (self.journal_id.l10n_py_authorization_id.name or "").strip().isdigit():
            errors.append(
                self.env._("El número de timbrado debe ser numérico para Sifende.")
            )
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

    # === Envío y respuesta ===

    def action_send_edi(self):
        self.ensure_one()
        connector = self._l10n_py_sifende_connector()
        if not connector:
            return super().action_send_edi()
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
                "l10n_py_edi_batch_id": cdc,
                "l10n_py_edi_message": self.env._(
                    "Enviado a Sifende; esperando la respuesta de SIFEN."
                ),
            }
        )
        self._l10n_py_generate_qr_image()

    def action_retry_edi(self):
        self.ensure_one()
        if (
            self.l10n_py_edi_status in ("error", "rejected")
            and self._l10n_py_sifende_connector()
            and not self.l10n_py_sifende_reintento_misma_clave
        ):
            self.l10n_py_sifende_intento += 1
        return super().action_retry_edi()

    def _generate_kude(self):
        self.ensure_one()
        connector = self._l10n_py_sifende_connector()
        if not connector:
            return super()._generate_kude()
        if not self.l10n_py_cdc:
            return
        pdf = connector.sifende_fetch_kude(self.l10n_py_cdc, document=self)
        self.write(
            {
                "l10n_py_kude_pdf": base64.b64encode(pdf),
                "l10n_py_kude_filename": f"KUDE_{self.l10n_py_cdc}.pdf",
            }
        )

    # === Cron ===

    @api.model
    def _cron_sifende_check_status(self):
        connectors = (
            self.env["l10n_py.edi.connector"]
            .sudo()
            .search([("provider_type", "=", "sifende")])
        )
        for connector in connectors:
            moves = self.sudo().search(
                [
                    ("company_id", "=", connector.company_id.id),
                    ("l10n_py_cdc", "!=", False),
                    "|",
                    ("l10n_py_edi_status", "=", "processing"),
                    ("l10n_py_sifende_archivos_pendientes", "=", True),
                ],
                limit=CRON_BATCH_SIZE,
                order="id",
            )
            for move in moves:
                try:
                    with self.env.cr.savepoint():
                        move._l10n_py_sifende_sync(connector)
                except Exception:
                    _logger.exception(
                        "Error consultando el estado en Sifende del documento %s",
                        move.name,
                    )

    def _l10n_py_sifende_sync(self, connector):
        self.ensure_one()
        if self.l10n_py_edi_status == "processing":
            response = connector.check_status(self.l10n_py_cdc)
            result = response.get("result") or {}
            state = result.get("estado")
            message = result.get("mensajeRechazo")
            if state in APPROVED_STATES:
                self.write(
                    {
                        "l10n_py_edi_status": "accepted",
                        "l10n_py_edi_message": message
                        or self.env._("Documento aprobado por SIFEN."),
                    }
                )
            elif state == "RECHAZADO":
                self.write(
                    {
                        "l10n_py_edi_status": "rejected",
                        "l10n_py_edi_message": message
                        or self.env._("SIFEN rechazó el documento."),
                        "l10n_py_sifende_reintento_misma_clave": False,
                    }
                )
                return
            elif state == "ERROR":
                self.write(
                    {
                        "l10n_py_edi_status": "error",
                        "l10n_py_edi_message": message
                        or self.env._(
                            "El envío falló en Sifende. Revise el panel de Sifende."
                        ),
                    }
                )
                return
            elif state == "CANCELADO":
                self.write({"l10n_py_edi_status": "cancelled"})
                return
            elif state in PENDING_STATES or not state:
                return
        if self.l10n_py_edi_status == "accepted":
            self._l10n_py_sifende_fetch_files(connector)

    def _l10n_py_sifende_fetch_files(self, connector):
        self.ensure_one()
        pending = False
        if not self.l10n_py_edi_xml:
            try:
                xml = connector.sifende_fetch_xml(self.l10n_py_cdc, document=self)
                self.write(
                    {
                        "l10n_py_edi_xml": base64.b64encode(xml),
                        "l10n_py_edi_xml_filename": f"{self.l10n_py_cdc}.xml",
                    }
                )
            except UserError as e:
                pending = True
                _logger.info("XML de %s pendiente: %s", self.l10n_py_cdc, e)
        if not self.l10n_py_kude_pdf:
            try:
                self._generate_kude()
            except UserError as e:
                pending = True
                _logger.info("KuDE de %s pendiente: %s", self.l10n_py_cdc, e)
        self.l10n_py_sifende_archivos_pendientes = pending
