from datetime import date

from odoo.tests import tagged

from ..services.request_builder import split_installments
from .common import FIXED_DATE, SifendeCommon


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestRequestMapping(SifendeCommon):
    def term(self, days_and_percents):
        return self.env["account.payment.term"].create(
            {
                "name": "Plan de cuotas",
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "value": "percent",
                            "value_amount": percent,
                            "delay_type": "days_after",
                            "nb_days": days,
                        },
                    )
                    for days, percent in days_and_percents
                ],
            }
        )

    def test_factura_contado(self):
        payload = self.sent_payload(self.make_move("fe"))
        self.assertEqual(payload, self.expected("fe_contado"))

    def test_receptor_envia_la_ciudad_por_nombre_no_por_codigo_de_oca(self):
        payload = self.sent_payload(self.make_move("fe"))
        self.assertEqual(payload["receptor"]["ciudad"], self.city_name)
        self.assertNotEqual(payload["receptor"]["ciudad"], self.city_code)

    def test_receptor_sin_telefono_envia_el_celular(self):
        self.partner.write({"phone": False, "mobile": "+595 981 123 456"})
        payload = self.sent_payload(self.make_move("fe"))
        self.assertEqual(payload["receptor"]["telefono"], "+595981123456")

    def test_receptor_de_capital_envia_asuncion(self):
        capital = self.env["res.country.state"].search(
            [("country_id", "=", self.country_py.id), ("l10n_py_code", "=", 1)],
            limit=1,
        )
        self.partner.write({"state_id": capital.id, "city_id": False, "city": "Barrio"})
        payload = self.sent_payload(self.make_move("fe"))
        self.assertEqual(payload["receptor"]["departamento"], "CAPITAL")
        self.assertEqual(payload["receptor"]["ciudad"], "Asunción")

    def test_factura_credito_en_cuotas_suma_exacta(self):
        term = self.term([(30, 33.33), (60, 33.33), (90, 33.34)])
        move = self.make_move(
            "fe", lines=[self.line(price=100000.0)], invoice_payment_term_id=term.id
        )
        payload = self.sent_payload(move)
        self.assertEqual(payload, self.expected("fe_credito_cuotas"))
        amounts = [c["monto"] for c in payload["credito"]["detalleCuotas"]]
        self.assertEqual(sum(amounts), 100000)

    def test_factura_credito_con_un_vencimiento_es_plazo(self):
        move = self.make_move("fe", invoice_payment_term_id=self.term([(30, 100)]).id)
        payload = self.sent_payload(move)
        self.assertEqual(payload["condicionOperacion"], "CREDITO")
        self.assertEqual(
            payload["credito"], {"condicionCredito": "PLAZO", "plazoCredito": "30 días"}
        )
        self.assertNotIn("pagos", payload)

    def test_pago_inmediato_se_emite_como_contado(self):
        move = self.make_move("fe", invoice_payment_term_id=self.term([(0, 100)]).id)
        payload = self.sent_payload(move)
        self.assertEqual(payload["condicionOperacion"], "CONTADO")
        self.assertEqual(payload["pagos"][0]["montoPago"], 11000)

    def test_pago_con_tarjeta_informa_tarjeta(self):
        move = self.make_move("fe", l10n_py_payment_type="3")
        payload = self.sent_payload(move)
        self.assertEqual(
            payload["pagos"][0]["tarjeta"],
            {
                "tipoTarjeta": "OTRO",
                "descripcionTipoTarjeta": "Otro",
                "formaProcesamientoPago": "OTRO",
            },
        )

    def test_unidad_de_medida_descuento_y_precio_con_iva(self):
        lines = [
            self.line(self.product_kg, qty=2.5, price=52500.0, discount=10.0),
            self.line(price=10000.0, tax=self.tax_10_excluded),
            self.line(price=5000.0, tax=self.tax_exempt),
        ]
        items = self.sent_payload(self.make_move("fe", lines=lines))["items"]
        self.assertEqual(items[0]["unidadMedida"], "kg")
        self.assertEqual(items[0]["cantidad"], 2.5)
        self.assertEqual(items[0]["descuentoParticularPorcentaje"], 10.0)
        self.assertEqual(items[1]["precioUnitario"], 11000)
        self.assertNotIn("descuentoParticularPorcentaje", items[1])
        self.assertEqual(items[2]["afectacionTributaria"], "EXENTO")
        self.assertEqual(items[2]["tasaIVA"], 0)

    def test_no_contribuyente_usa_tipo_de_documento_de_sifen(self):
        move = self.make_move("fe", partner_id=self.partner_nc.id)
        receptor = self.sent_payload(move)["receptor"]
        self.assertEqual(receptor["tipoContribuyente"], "NO_CONTRIBUYENTE")
        self.assertEqual(receptor["tipoDocumento"], "CEDULA_PARAGUAYA")
        self.assertEqual(receptor["numeroDocumento"], "4567890")
        self.assertNotIn("digitoVerificador", receptor)
        self.assertNotIn("tipoContribuyenteReceptor", receptor)

    def test_carnet_de_residencia_de_odoo_es_el_cuatro_de_sifen(self):
        self.partner_nc.l10n_py_doc_type = "3"
        receptor = self.sent_payload(
            self.make_move("fe", partner_id=self.partner_nc.id)
        )["receptor"]
        self.assertEqual(receptor["tipoDocumento"], "CARNET_DE_RESIDENCIA")

    def test_nota_de_credito(self):
        invoice = self.make_move("fe")
        move = self.make_move("nc", reversed_entry_id=invoice.id)
        self.associate(move)
        self.assertEqual(move.l10n_py_sifende_motivo_emision, "2")
        self.assertEqual(self.sent_payload(move), self.expected("nc"))

    def test_nota_de_credito_sin_reversion_usa_motivo_general(self):
        move = self.make_move("nc", posted=False)
        self.assertEqual(move.l10n_py_sifende_motivo_emision, "1")

    def test_nota_de_debito(self):
        move = self.make_move("nd")
        self.associate(move)
        self.assertEqual(move.l10n_py_sifende_motivo_emision, "8")
        self.assertEqual(self.sent_payload(move), self.expected("nd"))

    def test_nota_de_remision(self):
        move = self.make_move(
            "nr",
            l10n_py_nre_motive="5",
            l10n_py_sifende_km_recorrido=8,
            l10n_py_sifende_responsable_emision="3",
        )
        transport = self.env["l10n_py.transport"].create(
            {
                "move_id": move.id,
                "transport_mode": "1",
                "transport_type": "1",
                "freight_responsibility": "1",
                "transport_start_date": date(2026, 10, 21),
                "transport_end_date": date(2026, 10, 21),
                "departure_address": "Calle Teniente Benítez",
                "departure_house": 120,
                "departure_department": 12,
                "departure_city": int(self.city_code),
                "transporter_nature": "2",
                "transporter_name": "Carlos Benítez",
                "driver_doc_number": "3456789",
                "driver_name": "Carlos Benítez",
                "vehicle_ids": [
                    (
                        0,
                        0,
                        {
                            "vehicle_type": "CAMION",
                            "brand": "ISUZU",
                            "plate_number": "ABC1234",
                        },
                    )
                ],
                "delivery_ids": [
                    (
                        0,
                        0,
                        {
                            "address": "Avenida Mariscal López",
                            "house_number": 450,
                            "department": 12,
                            "city": int(self.city_code),
                        },
                    )
                ],
            }
        )
        move.l10n_py_transport_id = transport
        self.partner.l10n_py_ruc = self.company.l10n_py_ruc
        self.assertEqual(self.sent_payload(move), self.expected("nre"))

    def test_nota_de_remision_exige_kilometros_y_transporte(self):
        move = self.make_move("nr", l10n_py_nre_motive="2")
        with self.assertRaisesRegex(Exception, "kilómetros"):
            move.action_send_edi()

    def test_autofactura(self):
        move = self.make_move(
            "af",
            l10n_py_afe_constancia_type="1",
            l10n_py_afe_constancia_number="CONST-1",
            l10n_py_afe_constancia_control="CTRL-1",
            l10n_py_afe_vendor_doc_type="1",
            l10n_py_afe_vendor_doc_number="1234567",
            l10n_py_afe_vendor_name="María González",
            l10n_py_afe_vendor_address="Av. España",
            l10n_py_afe_vendor_house=0,
            l10n_py_afe_vendor_department=1,
            l10n_py_afe_vendor_district=1,
            l10n_py_afe_vendor_city=1,
            l10n_py_afe_provision_address="Ruta 1 km 20",
            l10n_py_afe_provision_department=12,
            l10n_py_afe_provision_district=2,
            l10n_py_afe_provision_city=3450,
        )
        self.env["l10n_py.associated.document"].create(
            {
                "move_id": move.id,
                "association_type": "3",
                "constancia_type": "1",
                "constancia_number": "CONST-1",
            }
        )
        self.assertEqual(self.sent_payload(move), self.expected("afe"))

    def test_la_fecha_de_emision_es_la_de_la_factura(self):
        payload = self.sent_payload(self.make_move("fe"))
        self.assertEqual(payload["fechaEmision"], f"{FIXED_DATE.isoformat()}T00:00:00")

    def test_los_valores_de_enum_salen_del_catalogo_de_la_api(self):
        self.enums["unidadMedida"][0]["name"] = "UNIDAD_RENOMBRADA"
        payload = self.sent_payload(self.make_move("fe"))
        self.assertEqual(payload["items"][0]["unidadMedida"], "UNIDAD_RENOMBRADA")

    def test_reparto_de_cuotas_en_guaranies(self):
        self.assertEqual(split_installments(100000, 3, 0), [33333, 33333, 33334])
        self.assertEqual(split_installments(1000000, 4, 0), [250000] * 4)
        self.assertEqual(split_installments(10, 3, 0), [3, 3, 4])

    def test_reparto_de_cuotas_con_decimales(self):
        amounts = split_installments(100.0, 3, 2)
        self.assertEqual(amounts, [33.33, 33.33, 33.34])
        self.assertAlmostEqual(sum(amounts), 100.0)
