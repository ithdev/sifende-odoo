from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import SifendeCommon


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestTaxConfiguration(SifendeCommon):
    def term(self, days):
        return self.env["account.payment.term"].create(
            {
                "name": f"Plazo {days} días",
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "value": "percent",
                            "value_amount": 100,
                            "delay_type": "days_after",
                            "nb_days": days,
                        },
                    )
                ],
            }
        )

    def group_tax(self, *children):
        return self.env["account.tax"].create(
            {
                "name": "Grupo IVA Test",
                "amount_type": "group",
                "type_tax_use": "sale",
                "children_tax_ids": [(6, 0, [tax.id for tax in children])],
            }
        )

    def fixed_tax(self):
        return self.env["account.tax"].create(
            {
                "name": "Fijo Test",
                "amount_type": "fixed",
                "amount": 500.0,
                "type_tax_use": "sale",
                "price_include_override": "tax_excluded",
            }
        )

    def assert_rejected_before_post(self, move, tax_name):
        self.route_emission(self.emission_response())
        with self.assertRaisesRegex(UserError, tax_name):
            move.action_send_edi()
        self.assertFalse(self.fake.calls_to("POST", ".*"))
        self.assertNotEqual(move.l10n_py_edi_status, "sent")

    def test_factura_a_plazo_con_grupo_de_impuestos_no_se_envia(self):
        group = self.group_tax(self.tax_10)
        move = self.make_move(
            "fe",
            lines=[self.line(tax=group)],
            invoice_payment_term_id=self.term(30).id,
        )
        self.assert_rejected_before_post(move, "Grupo IVA Test")

    def test_nota_de_credito_a_plazo_con_grupo_de_impuestos_no_se_envia(self):
        group = self.group_tax(self.tax_10)
        invoice = self.make_move("fe")
        move = self.make_move(
            "nc",
            lines=[self.line(tax=group)],
            reversed_entry_id=invoice.id,
            invoice_payment_term_id=self.term(30).id,
        )
        self.associate(move)
        self.assert_rejected_before_post(move, "Grupo IVA Test")

    def test_impuesto_fijo_no_se_envia(self):
        move = self.make_move("fe", lines=[self.line(tax=self.fixed_tax())])
        self.assert_rejected_before_post(move, "Fijo Test")

    def test_dos_impuestos_en_una_linea_no_se_envian(self):
        move = self.make_move(
            "fe", lines=[self.line(tax=self.tax_10 | self.tax_exempt)]
        )
        self.assert_rejected_before_post(move, "IVA 10% Test")

    def test_el_error_nombra_cada_linea_con_impuesto_no_soportado(self):
        group = self.group_tax(self.tax_10)
        move = self.make_move(
            "fe",
            lines=[self.line(), self.line(tax=group), self.line(tax=self.fixed_tax())],
        )
        with self.assertRaises(UserError) as caught:
            move.action_send_edi()
        message = str(caught.exception)
        self.assertIn("Grupo IVA Test", message)
        self.assertIn("Fijo Test", message)
        self.assertFalse(self.fake.calls_to("POST", ".*"))

    def test_un_impuesto_porcentual_unico_se_sigue_enviando(self):
        for tax in (self.tax_10, self.tax_5, self.tax_exempt, self.tax_10_excluded):
            move = self.make_move("fe", lines=[self.line(tax=tax)])
            self.assertTrue(self.sent_payload(move)["items"])
