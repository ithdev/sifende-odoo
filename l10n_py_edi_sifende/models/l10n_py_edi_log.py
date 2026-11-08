from odoo import api, fields, models


class EDILog(models.Model):
    _inherit = "l10n_py.edi.log"

    provider = fields.Selection(
        selection_add=[("sifende", "Sifende")],
        ondelete={"sifende": "cascade"},
    )
    company_id = fields.Many2one(
        "res.company",
        string="Empresa",
        index=True,
        ondelete="cascade",
        help="Empresa a la que pertenece la operación; los logs se aíslan por empresa.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Operaciones sin factura (estado, eventos, webhooks, otros proveedores)
        # llegan sin empresa: sin ella la regla de aislamiento las ocultaría.
        for vals in vals_list:
            if not vals.get("company_id"):
                document = self.env["account.move"].browse(vals.get("document_id"))
                vals["company_id"] = (document.company_id or self.env.company).id
        return super().create(vals_list)
