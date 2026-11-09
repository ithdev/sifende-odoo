import base64
import itertools
import threading
from contextlib import contextmanager
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.base.models.ir_actions_report import IrActionsReport

from .common import CDC, SifendeCommon

XML = b'<?xml version="1.0"?><rDE><DE Id="' + CDC.encode() + b'"/></rDE>'
PDF = b"%PDF-1.4 kude"
ODOO_PDF = b"%PDF-1.4 odoo"

STATUSES = (
    "draft",
    "to_send",
    "sent",
    "processing",
    "accepted",
    "rejected",
    "cancelled",
    "error",
)
MOVE_STATES = ("draft", "posted")
MOVE_TYPES = ("out_invoice", "out_refund", "in_invoice")
CONNECTORS = ("active", "archived", "absent")
CDCS = (True, False)
# estados en que el asiento pudo guardar el PDF del envío (el KuDE)
STORED_PDF_STATUSES = ("processing", "accepted", "cancelled")

CANCELLED = "cancelado en SIFEN"
PENDING = "todavía no tiene comprobante fiscal"


def expected(status, state, connector, move_type, has_cdc):
    """Qué se entrega de un documento, escrito desde la tabla de verdad y no desde
    los predicados del módulo.

    Es documento de Sifende un asiento de venta cuyo conector está activo, o
    archivado si el asiento ya fue emitido (tiene CDC). Aceptado entrega el KuDE
    (si tiene CDC); cancelado y publicado entrega el aviso de cancelación; todo
    lo demás espera la aceptación. Solo el aceptado queda sin motivo de bloqueo.
    """
    sale = move_type != "in_invoice"
    is_document = sale and (
        connector == "active" or (connector == "archived" and has_cdc)
    )
    pdf_kind, send_error, filetypes = "odoo", False, ()
    if is_document:
        if status == "accepted":
            if has_cdc:
                pdf_kind, filetypes = "kude", ("pdf", "xml")
        elif status == "cancelled":
            send_error = CANCELLED
            if state == "posted":
                pdf_kind, filetypes = "cancellation_notice", ("pdf",)
        else:
            send_error = PENDING
    return {
        "is_document": is_document,
        "pdf_kind": pdf_kind,
        "send_error": send_error,
        "legal_filetypes": filetypes,
    }


class DeliveryCommon(SifendeCommon):
    def setUp(self):
        super().setUp()
        patcher = patch.object(
            IrActionsReport, "_run_wkhtmltopdf", return_value=ODOO_PDF
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.company.external_report_layout_id = self.env.ref(
            "web.external_layout_standard"
        )
        purchase_journal = self.env["account.journal"].create(
            {
                "name": "Compras",
                "type": "purchase",
                "code": "COMP",
                "company_id": self.company.id,
            }
        )
        expense = self._account("expense", "600099", "Gastos")
        self._account(
            "liability_payable", "210099", "Cuentas por pagar", reconcile=True
        )
        # un asiento por (tipo, estado del asiento): cada celda cambia el resto
        self.moves = {}
        for move_type, state in itertools.product(MOVE_TYPES, MOVE_STATES):
            if move_type == "in_invoice":
                move = self.env["account.move"].create(
                    {
                        "move_type": "in_invoice",
                        "partner_id": self.partner.id,
                        "journal_id": purchase_journal.id,
                        "invoice_date": "2026-10-20",
                        "invoice_line_ids": [
                            (0, 0, self.line(account_id=expense.id, tax_ids=False))
                        ],
                    }
                )
                if state == "posted":
                    move.action_post()
            else:
                kind = "nc" if move_type == "out_refund" else "fe"
                move = self.make_move(kind, posted=state == "posted")
            self.assertEqual((move.move_type, move.state), (move_type, state))
            self.moves[move_type, state] = move

    def set_cell(self, move, status, has_cdc, stored):
        move.write(
            {
                # el PDF guardado al enviar: el KuDE, que un cancelado no debe servir
                "invoice_pdf_report_file": base64.b64encode(PDF) if stored else False,
                "l10n_py_edi_status": status,
                "l10n_py_cdc": CDC if has_cdc else False,
                # los archivos del KuDE están siempre: así ningún hook va a Sifende
                "l10n_py_kude_pdf": base64.b64encode(PDF),
                "l10n_py_kude_filename": f"KUDE_{CDC}.pdf",
                "l10n_py_edi_xml": base64.b64encode(XML),
                "l10n_py_edi_xml_filename": f"{CDC}.xml",
            }
        )
        move.invalidate_recordset(
            ["l10n_py_sifende_aviso_fiscal", "l10n_py_sifende_document_name"]
        )

    def set_connector(self, connector):
        if connector == "archived":
            self.connector.active = False
        elif connector == "absent":
            self.connector.unlink()
        self.env.flush_all()

    @contextmanager
    def recorded_queries(self):
        """Las consultas SQL que se hacen dentro del bloque (ya vaciado el caché)."""
        queries = []
        thread = threading.current_thread()
        hooks = getattr(thread, "query_hooks", [])
        self.env.flush_all()
        thread.query_hooks = [
            *hooks,
            lambda _cr, query, *_args: queries.append(str(query).strip()),
        ]
        try:
            yield queries
        finally:
            thread.query_hooks = hooks

    def cells(self):
        for status, state, move_type, has_cdc in itertools.product(
            STATUSES, MOVE_STATES, MOVE_TYPES, CDCS
        ):
            yield status, state, move_type, has_cdc, False
            if status in STORED_PDF_STATUSES:
                yield status, state, move_type, has_cdc, True


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestDeliveryMatrix(DeliveryCommon):
    """Fija qué entrega el módulo de cada documento, según su estado fiscal, el
    estado del asiento, el conector (activo, archivado o ausente) y el tipo.

    Se mira por los ganchos públicos de Odoo (impresión, portal, envío); la
    expectativa sale de ``expected``, no de los predicados.
    """

    def observe(self, move):
        """Lo que entregan los ganchos públicos para este asiento."""
        move = move.sudo().with_context(force_report_rendering=True)
        report = self.env.ref("account.account_invoices").with_context(
            force_report_rendering=True
        )
        stream = report._render_qweb_pdf_prepare_streams(report, {}, res_ids=move.ids)[
            move.id
        ]["stream"]
        observed = {
            "print": stream.getvalue(),
            "base_filename": move._get_report_base_filename(),
            "aviso": move.l10n_py_sifende_aviso_fiscal,
            "ready": move._is_ready_to_be_sent(),
            "portal_pdf": move._get_invoice_legal_documents("pdf", allow_fallback=True),
            "portal_all": move._get_invoice_legal_documents_all(allow_fallback=True),
        }
        try:
            move.action_send_and_print()
            observed["send"] = None
        except UserError as e:
            observed["send"] = str(e)
        move.action_print_pdf()
        return observed

    def check_cell(self, cell, connector):
        status, state, move_type, has_cdc, stored = cell
        move = self.moves[move_type, state]
        self.set_cell(move, status, has_cdc, stored)
        want = expected(status, state, connector, move_type, has_cdc)
        got = self.observe(move)
        kind, error = want["pdf_kind"], want["send_error"]
        # impresión: el KuDE solo para el aceptado; todo lo demás, el PDF de Odoo
        self.assertEqual(got["print"], PDF if kind == "kude" else ODOO_PDF)
        self.assertEqual(got["base_filename"].startswith("KUDE_"), kind == "kude")
        # portal y zip
        if kind == "kude":
            self.assertEqual(got["portal_pdf"]["content"], PDF)
            self.assertEqual(got["portal_pdf"]["filename"], f"KUDE_{CDC}.pdf")
            self.assertEqual(
                [(d["filename"], d["filetype"]) for d in got["portal_all"]],
                [(f"KUDE_{CDC}.pdf", "pdf"), (f"{CDC}.xml", "xml")],
            )
        elif kind == "cancellation_notice":
            # el KuDE guardado ya no vale: se sirve el PDF de Odoo con el aviso
            self.assertEqual(got["portal_pdf"]["content"], ODOO_PDF)
            self.assertFalse(got["portal_pdf"]["filename"].startswith("KUDE_"))
            self.assertEqual([d["content"] for d in got["portal_all"]], [ODOO_PDF])
            self.assertEqual([d["filetype"] for d in got["portal_all"]], ["pdf"])
        elif stored:
            # no es entrega del módulo: Odoo sirve el PDF que guardó
            self.assertEqual(got["portal_pdf"]["content"], PDF)
            self.assertNotIn("xml", [d["filetype"] for d in got["portal_all"]])
        else:
            self.assertEqual(got["portal_pdf"]["content"], ODOO_PDF)
            self.assertFalse(got["portal_pdf"]["filename"].startswith("KUDE_"))
            self.assertEqual([d["content"] for d in got["portal_all"]], [ODOO_PDF])
            self.assertEqual([d["filetype"] for d in got["portal_all"]], ["pdf"])
        # envío
        self.assertEqual(got["ready"], not error)
        if error and state == "posted":
            self.assertIn(error, got["send"])
        else:
            self.assertFalse(
                got["send"] and (CANCELLED in got["send"] or PENDING in got["send"])
            )
        # aviso del formulario
        if state == "posted" and error:
            self.assertIn(
                "Documento cancelado en SIFEN" if error == CANCELLED else "Sin validez",
                got["aviso"],
            )
        else:
            self.assertFalse(got["aviso"])
        self.assertFalse(self.fake.calls)

    def check_connector(self, connector):
        self.set_connector(connector)
        for cell in self.cells():
            with self.subTest(connector=connector, cell=cell):
                self.check_cell(cell, connector)

    def test_con_el_conector_activo(self):
        self.check_connector("active")

    def test_con_el_conector_archivado(self):
        self.check_connector("archived")

    def test_sin_conector(self):
        self.check_connector("absent")


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestDeliveryPredicates(DeliveryCommon):
    """Los predicados internos que hoy contestan la misma tabla de verdad."""

    def check_connector(self, connector):
        self.set_connector(connector)
        for status, state, move_type, has_cdc in self.cells_without_pdf():
            with self.subTest(
                connector=connector,
                cell=(status, state, move_type, has_cdc),
            ):
                move = self.moves[move_type, state].sudo()
                self.set_cell(move, status, has_cdc, False)
                want = expected(status, state, connector, move_type, has_cdc)
                self.assertEqual(
                    move._l10n_py_sifende_is_document(), want["is_document"]
                )
                self.assertEqual(
                    move._l10n_py_sifende_has_kude(), want["pdf_kind"] == "kude"
                )
                self.assertEqual(
                    move._l10n_py_sifende_is_cancelled_document(),
                    want["pdf_kind"] == "cancellation_notice",
                )
                error = move._l10n_py_sifende_send_error()
                self.assertEqual(bool(error), bool(want["send_error"]))
                if error:
                    self.assertIn(want["send_error"], error)
        self.assertFalse(self.fake.calls)

    def cells_without_pdf(self):
        return itertools.product(STATUSES, MOVE_STATES, MOVE_TYPES, CDCS)

    def test_con_el_conector_activo(self):
        self.check_connector("active")

    def test_con_el_conector_archivado(self):
        self.check_connector("archived")

    def test_sin_conector(self):
        self.check_connector("absent")


@tagged("post_install", "-at_install", "l10n_py_edi_sifende")
class TestDeliveryPolicy(DeliveryCommon):
    """La política de entrega contesta la tabla de verdad, sin red ni escrituras."""

    def check_connector(self, connector):
        self.set_connector(connector)
        for status, state, move_type, has_cdc in itertools.product(
            STATUSES, MOVE_STATES, MOVE_TYPES, CDCS
        ):
            with self.subTest(
                connector=connector, cell=(status, state, move_type, has_cdc)
            ):
                move = self.moves[move_type, state].sudo()
                self.set_cell(move, status, has_cdc, False)
                want = expected(status, state, connector, move_type, has_cdc)
                policy = move._sifende_delivery_policy()
                self.assertEqual(
                    set(policy), {"pdf_kind", "send_error", "legal_filetypes"}
                )
                self.assertEqual(policy["pdf_kind"], want["pdf_kind"])
                self.assertEqual(policy["legal_filetypes"], want["legal_filetypes"])
                self.assertEqual(bool(policy["send_error"]), bool(want["send_error"]))
                if want["send_error"]:
                    self.assertIn(want["send_error"], policy["send_error"])
                else:
                    self.assertIs(policy["send_error"], False)
        self.assertFalse(self.fake.calls)

    def test_con_el_conector_activo(self):
        self.check_connector("active")

    def test_con_el_conector_archivado(self):
        self.check_connector("archived")

    def test_sin_conector(self):
        self.check_connector("absent")

    def test_la_politica_no_escribe_ni_consulta_a_sifende(self):
        move = self.moves["out_invoice", "posted"]
        self.set_cell(move, "cancelled", True, True)
        move.invalidate_recordset()
        with self.recorded_queries() as queries:
            move._sifende_delivery_policy()
            self.env.flush_all()
        self.assertTrue(queries)
        writes = [q[:60] for q in queries if not q.upper().startswith("SELECT")]
        self.assertFalse(writes)
        self.assertFalse(self.fake.calls)
