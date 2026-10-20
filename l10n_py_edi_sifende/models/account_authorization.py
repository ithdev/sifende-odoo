from odoo import fields, models


class AccountAuthorization(models.Model):
    _inherit = "account.authorization"

    # l10n_py_edi_base los lee en la inutilización con estos nombres
    l10n_py_establishment = fields.Char(
        related="establishment", string="Establecimiento (OCA)"
    )
    l10n_py_point = fields.Char(
        related="expedition_point", string="Punto de expedición (OCA)"
    )
