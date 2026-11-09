from odoo import api, fields, models

from ..services import event_key


class EDICancelWizard(models.TransientModel):
    _inherit = "l10n_py.edi.cancel.wizard"

    # hay una cancelación con resultado incierto: se reenvía con su motivo original
    l10n_py_sifende_pending = fields.Boolean(compute="_compute_sifende_pending")

    @api.depends("invoice_id.l10n_py_sifende_event_request")
    def _compute_sifende_pending(self):
        for wizard in self:
            wizard.l10n_py_sifende_pending = bool(
                event_key.pending_request(wizard.invoice_id)
            )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if "motive" not in fields_list:
            return res
        invoice = self.env["account.move"].browse(res.get("invoice_id"))
        reason = event_key.pending_request(invoice).get("motivo")
        if reason:
            res["motive"] = reason
        return res

    def action_cancel(self):
        return super(
            EDICancelWizard, self.with_context(l10n_py_cancel_motive=self.motive)
        ).action_cancel()
