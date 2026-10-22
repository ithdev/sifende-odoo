from odoo.exceptions import UserError
from odoo.tests import tagged

from ..services.request_builder import SifendeRequestBuilder
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

    def test_receptor_envia_los_codigos_de_sifende_no_los_de_oca(self):
        receptor = self.sent_payload(self.make_move("fe"))["receptor"]
        self.assertEqual(self.city_code, "1107")
        self.assertEqual(receptor["ciudad"], "6106")
        self.assertEqual(receptor["codigoDistrito"], 169)

    def test_receptor_de_capital_envia_asuncion(self):
        capital = self.env["res.country.state"].search(
            [("country_id", "=", self.country_py.id), ("l10n_py_code", "=", 1)],
            limit=1,
        )
        self.partner.write({"state_id": capital.id, "city_id": False, "city": "Barrio"})
        receptor = self.sent_payload(self.make_move("fe"))["receptor"]
        self.assertEqual(receptor["departamento"], "CAPITAL")
        self.assertEqual(receptor["ciudad"], "1")
        self.assertEqual(receptor["codigoDistrito"], 1)

    def test_receptor_con_una_ciudad_que_sifende_no_reconoce_va_por_nombre(self):
        self.partner.write({"city_id": False, "city": "Ciudad Inventada"})
        receptor = self.sent_payload(self.make_move("fe"))["receptor"]
        self.assertEqual(receptor["departamento"], "CENTRAL")
        self.assertEqual(receptor["ciudad"], "Ciudad Inventada")
        self.assertNotIn("codigoDistrito", receptor)

    def test_receptor_con_ciudad_sifende_elegida_manda_esa(self):
        self.partner.write(
            {
                "city_id": False,
                "city": "Ciudad Inventada",
                "l10n_py_sifende_city_id": self.city_sifende.id,
            }
        )
        receptor = self.sent_payload(self.make_move("fe"))["receptor"]
        self.assertEqual(receptor["ciudad"], "6106")

    def test_receptor_de_un_departamento_con_otro_codigo_en_oca_va_con_el_de_sifende(
        self,
    ):
        state = self.env.ref("l10n_py_base.state_py_canindeyu")
        self.assertEqual(state.l10n_py_code, 15)
        self.partner.write(
            {"state_id": state.id, "city_id": False, "city": "Curuguaty"}
        )
        receptor = self.sent_payload(self.make_move("fe"))["receptor"]
        self.assertEqual(receptor["departamento"], "CANINDEYU")
        self.assertEqual(receptor["ciudad"], "4881")

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
        move = self.make_move(
            "fe", l10n_py_payment_type="3", l10n_py_sifende_tarjeta_marca="99"
        )
        payload = self.sent_payload(move)
        self.assertEqual(
            payload["pagos"][0]["tarjeta"],
            {
                "tipoTarjeta": "OTRO",
                "descripcionTipoTarjeta": "Otro",
                "formaProcesamientoPago": "OTRO",
            },
        )

    def test_pago_con_cheque_informa_numero_y_banco(self):
        move = self.make_move(
            "fe",
            l10n_py_payment_type="2",
            l10n_py_sifende_cheque_numero="1234",
            l10n_py_sifende_cheque_banco="  Banco Continental ",
        )
        pago = self.sent_payload(move)["pagos"][0]
        self.assertEqual(pago["tipoPago"], "CHEQUE")
        self.assertEqual(
            pago["cheque"],
            {"numeroCheque": "1234", "bancoCheque": "Banco Continental"},
        )

    def test_pago_con_cheque_exige_numero_y_banco_antes_de_enviar(self):
        move = self.make_move("fe", l10n_py_payment_type="2")
        with self.assertRaisesRegex(UserError, "número del cheque") as error:
            move.action_send_edi()
        self.assertIn("banco", str(error.exception))
        self.assertFalse(self.fake.calls_to("POST", ".*"))
        move.write(
            {
                "l10n_py_sifende_cheque_numero": "12 34",
                "l10n_py_sifende_cheque_banco": "BCP",
            }
        )
        with self.assertRaisesRegex(UserError, "solo dígitos"):
            move.action_send_edi()

    def test_el_cheque_de_una_factura_a_credito_no_se_exige(self):
        move = self.make_move(
            "fe",
            l10n_py_payment_type="2",
            invoice_payment_term_id=self.term([(30, 100)]).id,
        )
        payload = self.sent_payload(move)
        self.assertEqual(payload["condicionOperacion"], "CREDITO")

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
        self.assertEqual(self.sent_payload(self.make_nre()), self.expected("nre"))

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
            l10n_py_sifende_afe_vendor_city_id=self.city_sifende.id,
            l10n_py_afe_provision_address="Ruta 1 km 20",
            l10n_py_sifende_afe_provision_city_id=self.env["l10n_py.sifende.city"]
            .search([("code", "=", 2886)])
            .id,
        )
        self.assertEqual(self.sent_payload(move), self.expected("afe"))

    def test_la_fecha_de_emision_es_la_de_la_factura(self):
        payload = self.sent_payload(self.make_move("fe"))
        self.assertEqual(payload["fechaEmision"], f"{FIXED_DATE.isoformat()}T00:00:00")

    def test_los_valores_de_enum_salen_del_catalogo_de_la_api(self):
        self.enums["unidadMedida"][0]["name"] = "UNIDAD_RENOMBRADA"
        payload = self.sent_payload(self.make_move("fe"))
        self.assertEqual(payload["items"][0]["unidadMedida"], "UNIDAD_RENOMBRADA")

    def test_cuotas_desiguales_respetan_los_montos_de_las_cuentas_por_cobrar(self):
        move = self.make_move(
            "fe",
            lines=[self.line(price=100000.0)],
            invoice_payment_term_id=self.term([(30, 30), (60, 70)]).id,
        )
        receivable = move.line_ids.filtered(
            lambda line: line.display_type == "payment_term"
        )
        payload = self.sent_payload(move)
        installments = payload["credito"]["detalleCuotas"]
        self.assertEqual([c["monto"] for c in installments], [30000, 70000])
        self.assertEqual(
            [c["monto"] for c in installments],
            [line.amount_currency for line in receivable.sorted("date_maturity")],
        )
        self.assertEqual(sum(c["monto"] for c in installments), 100000)

    def test_dos_lineas_con_el_mismo_vencimiento_suman_una_cuota(self):
        term = self.term([(30, 40), (30, 30), (60, 30)])
        move = self.make_move(
            "fe", lines=[self.line(price=100000.0)], invoice_payment_term_id=term.id
        )
        credit = self.sent_payload(move)["credito"]
        self.assertEqual(credit["cuotas"], 2)
        self.assertEqual([c["monto"] for c in credit["detalleCuotas"]], [70000, 30000])

    def credit(self, *amounts):
        return {
            "infoCuotas": [
                {"monto": amount, "vencimiento": f"2026-1{index + 1}-19"}
                for index, amount in enumerate(amounts)
            ]
        }

    def builder(self):
        return SifendeRequestBuilder(self.env, lambda _category, _code: "CUOTA")

    def test_el_redondeo_de_las_cuotas_va_a_la_ultima(self):
        result = self.builder()._credito(
            self.credit(33333.33, 33333.33, 33333.33), {"montoTotal": 100000.0}, 2
        )
        self.assertEqual(
            [c["monto"] for c in result["detalleCuotas"]],
            [33333.33, 33333.33, 33333.34],
        )

    def test_las_cuotas_en_guaranies_son_enteras(self):
        result = self.builder()._credito(
            self.credit(30000.0, 70000.0), {"montoTotal": 100000.0}, 0
        )
        amounts = [c["monto"] for c in result["detalleCuotas"]]
        self.assertEqual(amounts, [30000, 70000])
        self.assertTrue(all(isinstance(amount, int) for amount in amounts))

    def test_unas_cuotas_que_no_suman_el_total_no_se_ajustan_en_silencio(self):
        with self.assertRaisesRegex(UserError, "no suman el total"):
            self.builder()._credito(
                self.credit(30000.0, 60000.0), {"montoTotal": 100000.0}, 0
            )

    def test_unos_items_desalineados_con_sus_datos_extra_no_se_ocultan(self):
        items = [{"codigo": "A"}, {"codigo": "B"}]
        self.assertEqual(
            len(list(SifendeRequestBuilder._zip_items({"items": items}, {}))), 2
        )
        with self.assertRaises(ValueError):
            list(SifendeRequestBuilder._zip_items({"items": items}, {"items": [{}]}))
