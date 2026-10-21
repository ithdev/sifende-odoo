from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestPartnerIdentity(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.country = cls.env.ref("base.py")
        cls.ci_type = cls.env.ref("l10n_py_base.it_ci")

    def partner_form(self, name):
        form = Form(self.env["res.partner"])
        form.name = name
        form.country_id = self.country
        return form

    def test_el_cliente_con_cedula_guarda_el_documento_desde_la_pantalla(self):
        form = self.partner_form("Ana González")
        form.l10n_latam_identification_type_id = self.ci_type
        form.vat = "1234567"
        partner = form.save()
        self.assertEqual(partner.l10n_py_taxpayer_type, "2")
        self.assertEqual(partner.l10n_py_doc_type, "1")
        self.assertEqual(partner.l10n_py_doc_number, "1234567")

    def test_el_innominado_se_puede_elegir_desde_la_pantalla(self):
        form = self.partner_form("Sin Nombre")
        form.l10n_py_taxpayer_type = "2"
        form.l10n_py_doc_type = "4"
        form.l10n_py_doc_number = "0"
        partner = form.save()
        self.assertEqual(partner.l10n_py_doc_type, "4")
        self.assertEqual(partner.l10n_py_doc_number, "0")
