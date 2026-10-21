from odoo import models


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def write(self, vals):
        if "date_maturity" in vals:
            for move in self.filtered(
                lambda line: line.display_type == "payment_term"
            ).move_id:
                if move._l10n_py_sifende_payment_frozen():
                    raise move._l10n_py_sifende_frozen_error()
        return super().write(vals)
