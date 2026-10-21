# pylint: disable=consider-merging-classes-inherited
import base64

from odoo import api, fields, models
from odoo.exceptions import UserError

SALE_MOVE_TYPES = ("out_invoice", "out_refund")


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_py_sifende_aviso_fiscal = fields.Char(
        compute="_compute_l10n_py_sifende_aviso_fiscal",
        string="Aviso fiscal (Sifende)",
    )

    @api.depends("state", "move_type", "company_id", "l10n_py_edi_status")
    def _compute_l10n_py_sifende_aviso_fiscal(self):
        for move in self:
            move.l10n_py_sifende_aviso_fiscal = False
            if (
                move.state != "posted"
                or move.l10n_py_edi_status == "accepted"
                or not move._l10n_py_sifende_is_document()
            ):
                continue
            if move.l10n_py_edi_status == "cancelled":
                move.l10n_py_sifende_aviso_fiscal = self.env._(
                    "Documento cancelado en SIFEN: sin validez fiscal."
                )
            else:
                move.l10n_py_sifende_aviso_fiscal = self.env._(
                    "Sin validez fiscal: SIFEN todavía no aceptó este documento. "
                    "El comprobante es el KuDE, que se entrega al figurar Aceptado."
                )

    def _l10n_py_sifende_is_document(self):
        self.ensure_one()
        return self.move_type in SALE_MOVE_TYPES and bool(
            self._l10n_py_sifende_connector()
        )

    def _l10n_py_sifende_has_kude(self):
        self.ensure_one()
        return (
            self.move_type in SALE_MOVE_TYPES
            and self.l10n_py_edi_status == "accepted"
            and bool(self.l10n_py_cdc)
            and self._l10n_py_sifende_is_document()
        )

    def _l10n_py_sifende_send_error(self):
        """Motivo por el que el documento no puede enviarse como comprobante fiscal."""
        self.ensure_one()
        if self.l10n_py_edi_status == "accepted" or not (
            self._l10n_py_sifende_is_document()
        ):
            return False
        if self.l10n_py_edi_status == "cancelled":
            return self.env._(
                "%(name)s fue cancelado en SIFEN: su KuDE ya no es un comprobante "
                "válido, por eso no se envía.",
                name=self.name,
            )
        status = dict(self._fields["l10n_py_edi_status"].selection)
        return self.env._(
            "%(name)s todavía no tiene comprobante fiscal: SIFEN aún no lo aceptó "
            "(estado: %(status)s). Envíelo cuando figure como Aceptado.",
            name=self.name,
            status=status.get(self.l10n_py_edi_status, self.l10n_py_edi_status),
        )

    def _l10n_py_sifende_ensure_files(self):
        self.ensure_one()
        if self.l10n_py_kude_pdf and self.l10n_py_edi_xml:
            return
        self.check_access("write")
        self._l10n_py_sifende_fetch_files(self._l10n_py_sifende_connector())

    def _l10n_py_sifende_missing_file_error(self, label):
        return UserError(
            self.env._(
                "Sifende todavía no entregó %(label)s de %(name)s. Puede tardar unos "
                "minutos: reintente en un momento. Si continúa, contacte a soporte.",
                label=label,
                name=self.name,
            )
        )

    def _l10n_py_sifende_kude_content(self):
        self.ensure_one()
        if not self.l10n_py_kude_pdf:
            self._l10n_py_sifende_ensure_files()
        if not self.l10n_py_kude_pdf:
            raise self._l10n_py_sifende_missing_file_error(self.env._("el KuDE"))
        return base64.b64decode(self.l10n_py_kude_pdf)

    def _l10n_py_sifende_xml_content(self):
        self.ensure_one()
        if not self.l10n_py_edi_xml:
            self._l10n_py_sifende_ensure_files()
        if not self.l10n_py_edi_xml:
            raise self._l10n_py_sifende_missing_file_error(self.env._("el XML firmado"))
        return base64.b64decode(self.l10n_py_edi_xml)

    def _l10n_py_sifende_xml_attachment(self):
        self.ensure_one()
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [
                    ("res_model", "=", "account.move"),
                    ("res_field", "=", "l10n_py_edi_xml"),
                    ("res_id", "=", self.id),
                ],
                limit=1,
            )
        )
        if attachment and attachment.name == attachment.res_field:
            attachment.name = self.l10n_py_edi_xml_filename or f"{self.l10n_py_cdc}.xml"
        return attachment

    def _l10n_py_sifende_prepare_send(self):
        """Deja listos el KuDE y el XML, y descarta un PDF de Odoo guardado antes."""
        self.ensure_one()
        kude = self._l10n_py_sifende_kude_content()
        self._l10n_py_sifende_xml_content()
        pdf = self.sudo().invoice_pdf_report_id
        if pdf and pdf.raw != kude:
            self.sudo().invoice_pdf_report_file = False

    def _get_report_base_filename(self):
        self.ensure_one()
        if self._l10n_py_sifende_has_kude():
            return f"KUDE_{self.l10n_py_cdc}"
        return super()._get_report_base_filename()

    def action_send_and_print(self):
        moves = self.filtered(lambda m: m.state == "posted")
        errors = [error for m in moves if (error := m._l10n_py_sifende_send_error())]
        if errors:
            raise UserError("\n".join(errors))
        if len(self) == 1 and self._l10n_py_sifende_has_kude():
            self._l10n_py_sifende_prepare_send()
        return super().action_send_and_print()

    def action_print_pdf(self):
        self.ensure_one()
        if self._l10n_py_sifende_has_kude():
            self._l10n_py_sifende_kude_content()
        return super().action_print_pdf()

    def _get_invoice_legal_documents(self, filetype, allow_fallback=False):
        self.ensure_one()
        if filetype == "pdf" and self._l10n_py_sifende_has_kude():
            if self.l10n_py_kude_pdf or allow_fallback:
                return self._l10n_py_sifende_kude_document()
        return super()._get_invoice_legal_documents(
            filetype, allow_fallback=allow_fallback
        )

    def _get_invoice_legal_documents_all(self, allow_fallback=False):
        self.ensure_one()
        if self._l10n_py_sifende_has_kude() and (
            allow_fallback or (self.l10n_py_kude_pdf and self.l10n_py_edi_xml)
        ):
            return [
                self._l10n_py_sifende_kude_document(),
                {
                    "filename": self.l10n_py_edi_xml_filename
                    or f"{self.l10n_py_cdc}.xml",
                    "filetype": "xml",
                    "content": self._l10n_py_sifende_xml_content(),
                },
            ]
        return super()._get_invoice_legal_documents_all(allow_fallback=allow_fallback)

    def _l10n_py_sifende_kude_document(self):
        return {
            "filename": f"{self._get_report_base_filename()}.pdf",
            "filetype": "pdf",
            "content": self._l10n_py_sifende_kude_content(),
        }
