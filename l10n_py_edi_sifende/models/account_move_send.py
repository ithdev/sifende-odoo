from odoo import api, models
from odoo.exceptions import UserError


class AccountMoveSend(models.AbstractModel):
    _inherit = "account.move.send"

    @api.model
    def _get_invoice_extra_attachments(self, move):
        attachments = super()._get_invoice_extra_attachments(move)
        if move._l10n_py_sifende_has_kude():
            attachments |= move._l10n_py_sifende_xml_attachment()
        return attachments

    @api.model
    def _hook_invoice_document_before_pdf_report_render(self, invoice, invoice_data):
        super()._hook_invoice_document_before_pdf_report_render(invoice, invoice_data)
        if not invoice._l10n_py_sifende_is_document():
            return
        error = invoice._l10n_py_sifende_send_error()
        if not error:
            try:
                invoice._l10n_py_sifende_prepare_send()
            except UserError as e:
                error = str(e)
        if error:
            invoice_data["error"] = {
                "error_title": self.env._("No se puede enviar el comprobante fiscal:"),
                "errors": [error],
            }
