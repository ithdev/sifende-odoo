from odoo import fields, models


class EDILog(models.Model):
    _inherit = "l10n_py.edi.log"

    provider = fields.Selection(
        selection_add=[("sifende", "Sifende")],
        ondelete={"sifende": "cascade"},
    )
