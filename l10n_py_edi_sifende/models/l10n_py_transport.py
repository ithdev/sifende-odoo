from odoo import api, fields, models

DOCUMENT_TYPES = [
    ("1", "Cédula paraguaya"),
    ("2", "Pasaporte"),
    ("3", "Cédula extranjera"),
    ("4", "Carnet de residencia"),
]


class Transport(models.Model):
    _inherit = "l10n_py.transport"

    # el diálogo de "Crear y editar" no conoce el documento; se enlaza al guardarlo
    move_id = fields.Many2one(required=False)
    freight_responsibility = fields.Selection(
        selection_add=[("5", "Transporte propio (E905)")]
    )
    sifende_transporter_document_type = fields.Selection(
        DOCUMENT_TYPES, string="Tipo de documento del transportista (E985)"
    )
    sifende_transporter_document_number = fields.Char(
        string="Documento del transportista (E987)", size=20
    )
    sifende_transporter_address = fields.Char(
        string="Domicilio fiscal del transportista (E992)", size=150
    )
    sifende_driver_address = fields.Char(string="Dirección del chofer (E993)", size=255)
    sifende_agent_name = fields.Char(string="Nombre del agente (E994)", size=60)
    sifende_agent_ruc = fields.Char(string="RUC del agente (E995)", size=8)
    sifende_agent_dv = fields.Char(string="DV del agente (E996)", size=1)
    sifende_agent_address = fields.Char(string="Dirección del agente (E997)", size=255)
    sifende_import_dispatch = fields.Char(
        string="Despacho de importación (E908)", size=16
    )
    sifende_departure_city_id = fields.Many2one(
        "l10n_py.sifende.city",
        string="Ciudad de salida",
        compute="_compute_sifende_departure_city_id",
        inverse="_inverse_sifende_departure_city_id",
    )

    @api.depends("departure_city")
    def _compute_sifende_departure_city_id(self):
        cities = self.env["l10n_py.sifende.city"]
        for transport in self:
            transport.sifende_departure_city_id = cities._sifende_by_code(
                transport.departure_city
            )

    def _inverse_sifende_departure_city_id(self):
        for transport in self:
            department, district, city = (
                transport.sifende_departure_city_id._sifende_ints()
            )
            transport.departure_department = department
            transport.departure_district = district
            transport.departure_city = city

    @api.depends("move_id.name", "transporter_name")
    def _compute_display_name(self):
        for transport in self:
            parts = [transport.move_id.name, transport.transporter_name]
            transport.display_name = " - ".join(p for p in parts if p) or self.env._(
                "Transporte nuevo"
            )


class TransportDelivery(models.Model):
    _inherit = "l10n_py.transport.delivery"

    # se completan desde la ciudad elegida, después del INSERT
    department = fields.Integer(required=False)
    city = fields.Integer(required=False)
    sifende_city_id = fields.Many2one(
        "l10n_py.sifende.city",
        string="Ciudad",
        compute="_compute_sifende_city_id",
        inverse="_inverse_sifende_city_id",
    )

    @api.depends("city")
    def _compute_sifende_city_id(self):
        cities = self.env["l10n_py.sifende.city"]
        for delivery in self:
            delivery.sifende_city_id = cities._sifende_by_code(delivery.city)

    def _inverse_sifende_city_id(self):
        for delivery in self:
            delivery.department, delivery.district, delivery.city = (
                delivery.sifende_city_id._sifende_ints()
            )
