from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import SifendeCommon


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestEmissionGuards(SifendeCommon):
    def test_un_borrador_no_se_envia(self):
        move = self.make_move("fe", posted=False)
        with self.assertRaisesRegex(UserError, "Confirme la factura"):
            move.action_send_edi()
        self.assertFalse(self.fake.calls_to("POST", ".*"))
        self.assertNotEqual(move.l10n_py_edi_status, "sent")

    def test_un_borrador_sin_fecha_no_salta_un_type_error(self):
        move = self.make_move("fe", posted=False)
        move.invoice_date = False
        with self.assertRaisesRegex(UserError, "Confirme la factura"):
            move.action_send_edi()
        errors = move._validate_edi_document_type()
        self.assertTrue(any("fecha" in error for error in errors))

    def test_un_borrador_no_se_reintenta(self):
        move = self.make_move("fe", posted=False)
        move.l10n_py_edi_status = "error"
        with self.assertRaisesRegex(UserError, "Confirme la factura"):
            move.action_retry_edi()
        self.assertEqual(move.l10n_py_sifende_intento, 0)
        self.assertFalse(self.fake.calls_to("POST", ".*"))

    def test_una_factura_cancelada_no_se_envia(self):
        move = self.make_move("fe")
        move.button_draft()
        move.button_cancel()
        with self.assertRaisesRegex(UserError, "Confirme la factura"):
            move.action_send_edi()

    def test_una_factura_publicada_se_envia(self):
        move = self.make_move("fe")
        self.route_emission(self.emission_response())
        move.action_send_edi()
        self.assertEqual(move.l10n_py_edi_status, "processing")

    def test_los_botones_de_envio_se_ocultan_en_borrador(self):
        arch = self.env["account.move"].get_view(
            self.env.ref("account.view_move_form").id
        )["arch"]
        tree = etree.fromstring(arch)
        for name in ("action_send_edi", "action_retry_edi"):
            button = tree.xpath(f"//button[@name='{name}']")[0]
            self.assertIn("state", button.get("invisible"), name)
