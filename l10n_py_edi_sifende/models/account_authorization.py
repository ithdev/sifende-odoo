from odoo import api, fields, models


class AccountAuthorization(models.Model):
    _inherit = "account.authorization"

    # l10n_py_edi_base los lee en la inutilización con estos nombres
    l10n_py_establishment = fields.Char(
        related="establishment", string="Establecimiento (OCA)"
    )
    l10n_py_point = fields.Char(
        related="expedition_point", string="Punto de expedición (OCA)"
    )
    l10n_py_sifende_ultimo_numero_en_uso = fields.Integer(
        string="Último número en uso en Sifende",
        copy=False,
        help="Mayor número que Sifende informó como ya usado o inutilizado en este "
        "punto de expedición. La numeración no lo reutiliza ni retrocede.",
    )

    def _l10n_py_sifende_next_number(self, exclude_move=None):
        self.ensure_one()
        domain = [
            ("l10n_py_authorization_id", "=", self.id),
            ("l10n_py_invoice_number", ">", 0),
            ("move_type", "in", ("out_invoice", "out_refund")),
        ]
        if exclude_move:
            domain.append(("id", "!=", exclude_move.id))
        last = (
            self.env["account.move"]
            .sudo()
            ._read_group(domain, aggregates=["l10n_py_invoice_number:max"])[0][0]
            or 0
        )
        number = max(
            last + 1,
            self.invoice_number_from,
            self.l10n_py_sifende_ultimo_numero_en_uso + 1,
        )
        # SIFEN no admite documentos con un número inutilizado: se salta cada
        # rango aceptado que contenga al candidato, también los encadenados.
        # También cuentan los enviados sin confirmar (estado enviado, o
        # borrador con la clave de envío pendiente), porque pueden haberse
        # aceptado en Sifende; los rechazados no
        voided = (
            self.env["l10n_py.number.inutilization"]
            .sudo()
            .search(
                [
                    ("authorization_id", "=", self.id),
                    "|",
                    ("state", "in", ("accepted", "sent")),
                    "&",
                    ("state", "=", "draft"),
                    ("l10n_py_sifende_event_key", "!=", False),
                    ("number_to", ">=", number),
                ],
                order="number_from, number_to",
            )
        )
        for inutilization in voided:
            if inutilization.number_from <= number <= inutilization.number_to:
                number = inutilization.number_to + 1
        return number

    @api.depends("invoice_number_from", "l10n_py_sifende_ultimo_numero_en_uso")
    def _compute_next_number(self):
        res = super()._compute_next_number()
        for authorization in self:
            authorization.next_number = authorization._l10n_py_sifende_next_number()
        return res
