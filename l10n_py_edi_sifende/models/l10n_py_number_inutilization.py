from odoo import models


class NumberInutilization(models.Model):
    _inherit = "l10n_py.number.inutilization"

    def action_send(self):
        return super(
            NumberInutilization, self.with_context(l10n_py_inutilization_id=self.id)
        ).action_send()
