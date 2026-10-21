from lxml import etree

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import CDC, FakeResponse, SifendeCommon

EVENT_OK = FakeResponse(200, {"estadoEvento": "APROBADO"})


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestProcedures(SifendeCommon):
    def make_user(self, login, *groups):
        return self.env["res.users"].create(
            {
                "name": login,
                "login": login,
                "groups_id": [
                    (
                        6,
                        0,
                        [
                            self.env.ref(group).id
                            for group in ("base.group_user", *groups)
                        ],
                    )
                ],
            }
        )

    def accepted_move(self):
        move = self.make_move("fe")
        move.write({"l10n_py_edi_status": "accepted", "l10n_py_cdc": CDC})
        return move

    def inutilization(self, number_from, number_to=None):
        return self.env["l10n_py.number.inutilization"].create(
            {
                "authorization_id": self.auths["fe"].id,
                "number_from": number_from,
                "number_to": number_to or number_from,
                "motive": "Documento rechazado que cambió",
            }
        )

    # === Cancelación ===

    def cancel_button(self, user=None):
        move = self.env["account.move"]
        if user:
            move = move.with_user(user)
        arch = move.get_view(view_type="form")["arch"]
        buttons = etree.fromstring(arch).xpath("//button[@string='Cancelar EDI']")
        return buttons[0] if buttons else None

    def test_el_boton_cancelar_edi_abre_el_asistente_del_motivo(self):
        action = self.env.ref("l10n_py_edi_sifende.action_l10n_py_edi_cancel_wizard")
        button = self.cancel_button()
        self.assertEqual(button.get("type"), "action")
        self.assertEqual(button.get("name"), str(action.id))
        self.assertEqual(action.res_model, "l10n_py.edi.cancel.wizard")
        self.assertEqual(action.target, "new")

    def test_el_boton_cancelar_edi_es_de_facturacion(self):
        biller = self.make_user("facturador_ui", "account.group_account_invoice")
        reader = self.make_user("auditor_ui", "account.group_account_readonly")
        self.assertIsNotNone(self.cancel_button(biller))
        self.assertIsNone(self.cancel_button(reader))

    def test_cancelar_desde_el_asistente_pide_el_motivo_y_lo_envia(self):
        move = self.accepted_move()
        self.fake.add("POST", "/cancelar$", EVENT_OK)
        action = self.env.ref("l10n_py_edi_sifende.action_l10n_py_edi_cancel_wizard")
        wizard = (
            self.env[action.res_model]
            .with_context(active_id=move.id, active_model="account.move")
            .create({"motive": "Error en el monto facturado"})
        )
        self.assertEqual(wizard.invoice_id, move)
        wizard.action_cancel()
        self.assertEqual(move.l10n_py_edi_status, "cancelled")
        self.assertEqual(
            self.fake.calls_to("POST", "/cancelar$")[0].json,
            {"motivo": "Error en el monto facturado"},
        )

    def test_en_sandbox_el_boton_cancelar_edi_se_oculta(self):
        move = self.accepted_move()
        self.assertFalse(move.l10n_py_sifende_sandbox)
        self.connector.sifende_api_key = "sk_sandbox_clave-ficticia"
        move.invalidate_recordset()
        self.assertTrue(move.l10n_py_sifende_sandbox)
        self.assertIn("l10n_py_sifende_sandbox", self.cancel_button().get("invisible"))

    def test_el_ambiente_sandbox_que_informa_sifende_tambien_cuenta(self):
        move = self.accepted_move()
        self.connector.sifende_ambiente = "SANDBOX"
        self.assertTrue(move.l10n_py_sifende_sandbox)

    def test_cancelar_en_sandbox_avisa_sin_llamar_a_sifende(self):
        move = self.accepted_move()
        self.connector.sifende_api_key = "sk_sandbox_clave-ficticia"
        with self.assertRaisesRegex(UserError, "SANDBOX"):
            move.action_cancel_edi()
        self.assertFalse(self.fake.calls_to("POST", ".*"))
        self.assertEqual(move.l10n_py_edi_status, "accepted")

    # === Inutilización ===

    def test_la_inutilizacion_tiene_menu_para_el_responsable_contable(self):
        action = self.env.ref("l10n_py_edi_base.action_l10n_py_number_inutilization")
        domain = [("action", "=", f"ir.actions.act_window,{action.id}")]
        menu = self.env["ir.ui.menu"].search(domain)
        root = self.env.ref("l10n_py_edi_base.menu_l10n_py_edi_root")
        self.assertEqual(menu.parent_id, root)
        self.assertEqual(root.parent_id, self.env.ref("account.menu_finance"))
        self.assertEqual(
            menu.complete_name.split("/", 1)[1],
            "Facturación Electrónica/Inutilización de números",
        )
        manager = self.make_user("gerente_ui", "account.group_account_manager")
        biller = self.make_user("facturador_menu", "account.group_account_invoice")
        self.assertIn(menu.id, menu.with_user(manager)._visible_menu_ids())
        self.assertNotIn(menu.id, menu.with_user(biller)._visible_menu_ids())

    def test_se_puede_inutilizar_el_numero_de_un_documento_rechazado(self):
        move = self.make_move("fe")
        move.l10n_py_edi_status = "rejected"
        self.inutilization(move.l10n_py_invoice_number)

    def test_no_se_puede_inutilizar_el_numero_de_un_documento_vigente(self):
        for status in ("to_send", "sent", "processing", "accepted", "cancelled"):
            move = self.make_move("fe")
            move.l10n_py_edi_status = status
            with self.assertRaisesRegex(ValidationError, "ya fueron usados"):
                self.inutilization(move.l10n_py_invoice_number)

    def test_procedimiento_de_un_rechazado_que_cambio(self):
        rejected = self.make_move("fe")
        rejected.l10n_py_edi_status = "rejected"
        number = rejected.l10n_py_invoice_number
        inutilization = self.inutilization(number)
        self.fake.add("POST", "/inutilizar$", EVENT_OK)
        inutilization.action_send()
        self.assertEqual(inutilization.state, "accepted")
        rejected.button_draft()
        rejected.button_cancel()
        self.assertEqual(rejected.state, "cancel")
        replacement = self.make_move("fe")
        self.assertEqual(replacement.l10n_py_invoice_number, number + 1)
