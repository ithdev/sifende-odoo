from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import CDC, SifendeCommon


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestFrozenPayment(SifendeCommon):
    def term(self):
        return self.env["account.payment.term"].create(
            {
                "name": "30/70",
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
                    for days, percent in ((30, 30), (60, 70))
                ],
            }
        )

    def cheque_move(self, **vals):
        return self.make_move(
            "fe",
            l10n_py_payment_type="2",
            l10n_py_sifende_cheque_numero="1234",
            l10n_py_sifende_cheque_banco="Banco Continental",
            **vals,
        )

    def sent(self, move):
        self.route_emission(self.emission_response())
        move.action_send_edi()
        return move

    def test_los_datos_de_pago_se_pueden_editar_antes_de_enviar(self):
        move = self.cheque_move()
        move.write(
            {
                "l10n_py_payment_type": "1",
                "l10n_py_sifende_cheque_numero": "99",
                "l10n_py_sifende_cheque_banco": "Otro banco",
            }
        )
        self.assertEqual(move.l10n_py_payment_type, "1")

    def test_el_tipo_de_pago_no_se_edita_con_el_documento_procesando(self):
        move = self.sent(self.cheque_move())
        self.assertEqual(move.l10n_py_edi_status, "processing")
        with self.assertRaisesRegex(UserError, "datos de pago"):
            move.l10n_py_payment_type = "1"
        self.assertEqual(move.l10n_py_payment_type, "2")

    def test_el_cheque_no_se_edita_con_el_documento_procesando(self):
        move = self.sent(self.cheque_move())
        with self.assertRaisesRegex(UserError, "datos de pago"):
            move.l10n_py_sifende_cheque_numero = "5678"
        with self.assertRaisesRegex(UserError, "datos de pago"):
            move.l10n_py_sifende_cheque_banco = "Banco Itaú"

    def test_los_datos_de_pago_no_se_editan_con_el_documento_aceptado(self):
        move = self.cheque_move()
        move.write({"l10n_py_edi_status": "accepted", "l10n_py_cdc": CDC})
        with self.assertRaisesRegex(UserError, "datos de pago"):
            move.l10n_py_payment_type = "1"

    def test_un_documento_con_cdc_rechazado_tampoco_se_edita(self):
        move = self.cheque_move()
        move.write({"l10n_py_edi_status": "rejected", "l10n_py_cdc": CDC})
        with self.assertRaisesRegex(UserError, "datos de pago"):
            move.l10n_py_sifende_cheque_banco = "Banco Itaú"

    def test_el_mismo_valor_no_cuenta_como_cambio(self):
        move = self.sent(self.cheque_move())
        move.write(
            {"l10n_py_payment_type": "2", "l10n_py_sifende_cheque_numero": "1234"}
        )

    def test_un_envio_rechazado_sin_cdc_se_puede_corregir(self):
        move = self.cheque_move()
        move.write({"l10n_py_edi_status": "rejected"})
        move.l10n_py_sifende_cheque_numero = "5678"
        self.assertEqual(move.l10n_py_sifende_cheque_numero, "5678")

    def test_la_condicion_de_pago_no_se_edita_con_el_documento_procesando(self):
        move = self.sent(self.make_move("fe"))
        with self.assertRaisesRegex(UserError, "datos de pago"):
            move.invoice_payment_term_id = self.term()

    def test_los_vencimientos_de_las_cuotas_no_se_editan_una_vez_enviadas(self):
        move = self.sent(self.make_move("fe", invoice_payment_term_id=self.term().id))
        line = move.line_ids.filtered(lambda line: line.display_type == "payment_term")[
            0
        ]
        with self.assertRaisesRegex(UserError, "datos de pago"):
            line.date_maturity = line.date_maturity.replace(year=2030)

    def test_los_vencimientos_se_editan_antes_de_enviar(self):
        move = self.make_move("fe", invoice_payment_term_id=self.term().id)
        line = move.line_ids.filtered(lambda line: line.display_type == "payment_term")[
            0
        ]
        line.date_maturity = line.date_maturity.replace(year=2030)
        self.assertEqual(line.date_maturity.year, 2030)

    def test_la_vista_marca_los_datos_de_pago_como_solo_lectura(self):
        arch = self.env["account.move"].get_view(
            self.env.ref("account.view_move_form").id
        )["arch"]
        tree = etree.fromstring(arch)
        for name in (
            "l10n_py_payment_type",
            "l10n_py_sifende_cheque_numero",
            "l10n_py_sifende_cheque_banco",
        ):
            node = tree.xpath(f"//field[@name='{name}']")[0]
            self.assertIn("processing", node.get("readonly"), name)
