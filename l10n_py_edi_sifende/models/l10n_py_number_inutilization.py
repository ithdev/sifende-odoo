from odoo import api, models
from odoo.exceptions import ValidationError


class NumberInutilization(models.Model):
    _inherit = "l10n_py.number.inutilization"

    def action_send(self):
        self.check_access("write")
        return super(
            NumberInutilization, self.with_context(l10n_py_inutilization_id=self.id)
        ).action_send()

    # SIFEN acepta inutilizar el número de un documento rechazado
    @api.constrains("number_from", "number_to", "authorization_id")
    def _check_no_used_numbers(self):
        for rec in self:
            used = self.env["account.move"].search_count(
                [
                    ("l10n_py_authorization_id", "=", rec.authorization_id.id),
                    ("l10n_py_invoice_number", ">=", rec.number_from),
                    ("l10n_py_invoice_number", "<=", rec.number_to),
                    ("state", "=", "posted"),
                    ("l10n_py_edi_status", "!=", "rejected"),
                ]
            )
            if used:
                raise ValidationError(
                    self.env._(
                        "Hay %(count)s número(s) en el rango que ya fueron usados "
                        "en facturas confirmadas y no fueron rechazadas por SIFEN.",
                        count=used,
                    )
                )
