# pylint: disable=consider-merging-classes-inherited
import base64

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError

SALE_MOVE_TYPES = ("out_invoice", "out_refund")


class AccountMove(models.Model):
    _inherit = "account.move"

    l10n_py_sifende_aviso_fiscal = fields.Char(
        compute="_compute_l10n_py_sifende_aviso_fiscal",
        string="Aviso fiscal (Sifende)",
    )

    @api.depends("state", "move_type", "company_id", "l10n_py_edi_status")
    def _compute_l10n_py_sifende_aviso_fiscal(self):
        for move in self:
            move.l10n_py_sifende_aviso_fiscal = False
            # un aceptado no tiene qué avisar: no hace falta buscar el conector
            if move.state != "posted" or move.l10n_py_edi_status == "accepted":
                continue
            policy = move._sifende_delivery_policy()
            if policy["pdf_kind"] == "cancellation_notice":
                move.l10n_py_sifende_aviso_fiscal = self.env._(
                    "Documento cancelado en SIFEN: sin validez fiscal."
                )
            elif policy["send_error"]:
                move.l10n_py_sifende_aviso_fiscal = self.env._(
                    "Sin validez fiscal: SIFEN todavía no aceptó este documento. "
                    "El comprobante es el KuDE, que se entrega al figurar Aceptado."
                )

    def _l10n_py_sifende_is_document(self):
        self.ensure_one()
        if self.move_type not in SALE_MOVE_TYPES:
            return False
        if self._l10n_py_sifende_connector():
            return True
        # archivar el conector no cambia quién emitió un documento ya enviado
        return bool(self.l10n_py_cdc) and bool(
            self.env["l10n_py.edi.connector"]
            .sudo()
            .with_context(active_test=False)
            .search_count(
                [
                    ("company_id", "=", self.company_id.id),
                    ("provider_type", "=", "sifende"),
                ],
                limit=1,
            )
        )

    def _sifende_delivery_policy(self):
        """Qué se entrega de este documento, sin hablar con Sifende ni escribir.

        - ``pdf_kind``: ``kude`` (el PDF es el KuDE aceptado), ``cancellation_notice``
          (el PDF de Odoo con el aviso de cancelación) u ``odoo`` (el de siempre).
        - ``send_error``: por qué no puede enviarse como comprobante fiscal, o False.
        - ``legal_filetypes``: lo que se agrega a los documentos legales del portal y
          de la descarga; vacío es dejar que lo resuelva Odoo.

        Solo lee campos guardados y la existencia del conector (activo o archivado,
        con sudo), así que sirve igual desde el portal. El contenido del KuDE y del
        XML se trae aparte: la política dice qué, no lo trae.
        """
        self.ensure_one()
        policy = {"pdf_kind": "odoo", "send_error": False, "legal_filetypes": ()}
        if not self._l10n_py_sifende_is_document():
            return policy
        status = self.l10n_py_edi_status
        if status == "accepted":
            if self.l10n_py_cdc:
                policy.update(pdf_kind="kude", legal_filetypes=("pdf", "xml"))
        elif status == "cancelled":
            policy["send_error"] = self.env._(
                "%(name)s fue cancelado en SIFEN: su KuDE ya no es un comprobante "
                "válido, por eso no se envía.",
                name=self.name,
            )
            if self.state == "posted":
                # el PDF de Odoo lleva el aviso; el KuDE guardado ya no vale
                policy.update(pdf_kind="cancellation_notice", legal_filetypes=("pdf",))
        else:
            policy["send_error"] = self.env._(
                "%(name)s todavía no tiene comprobante fiscal: SIFEN aún no lo aceptó "
                "(estado: %(status)s). Envíelo cuando figure como Aceptado.",
                name=self.name,
                status=dict(self._fields["l10n_py_edi_status"].selection).get(
                    status, status
                ),
            )
        return policy

    def _l10n_py_sifende_cancelled_document(self):
        # el PDF de Odoo lleva el aviso de cancelación; el KuDE guardado ya no vale
        report = self.env["ir.actions.report"]
        content, report_type = report._pre_render_qweb_pdf(
            "account.account_invoices", self.ids
        )
        content_by_id = report._get_splitted_report(
            "account.account_invoices", content, report_type
        )
        return {
            "filename": self._get_invoice_report_filename(),
            "filetype": "pdf",
            "content": content_by_id[self.id],
        }

    def _l10n_py_sifende_ensure_files(self):
        self.ensure_one()
        if self.l10n_py_kude_pdf and self.l10n_py_edi_xml:
            return
        connector = self._l10n_py_sifende_connector()
        if not connector:
            # conector archivado: no hay a quién pedirle los archivos; quien llama
            # avisa que faltan
            return
        self.check_access("write")
        self._l10n_py_sifende_fetch_files(connector)

    def _l10n_py_sifende_missing_file_error(self, label):
        return UserError(
            self.env._(
                "Sifende todavía no entregó %(label)s de %(name)s. Puede tardar unos "
                "minutos: reintente en un momento. Si continúa, contacte a soporte.",
                label=label,
                name=self.name,
            )
        )

    def _l10n_py_sifende_kude_content(self):
        self.ensure_one()
        if not self.l10n_py_kude_pdf:
            self._l10n_py_sifende_ensure_files()
        if not self.l10n_py_kude_pdf:
            raise self._l10n_py_sifende_missing_file_error(self.env._("el KuDE"))
        return base64.b64decode(self.l10n_py_kude_pdf)

    def _l10n_py_sifende_xml_content(self):
        self.ensure_one()
        if not self.l10n_py_edi_xml:
            self._l10n_py_sifende_ensure_files()
        if not self.l10n_py_edi_xml:
            raise self._l10n_py_sifende_missing_file_error(self.env._("el XML firmado"))
        return base64.b64decode(self.l10n_py_edi_xml)

    def _l10n_py_sifende_xml_attachment(self):
        self.ensure_one()
        attachment = (
            self.env["ir.attachment"]
            .with_user(SUPERUSER_ID)
            .search(
                [
                    ("res_model", "=", "account.move"),
                    ("res_field", "=", "l10n_py_edi_xml"),
                    ("res_id", "=", self.id),
                ],
                limit=1,
            )
        )
        if attachment and attachment.name == attachment.res_field:
            attachment.name = self.l10n_py_edi_xml_filename or f"{self.l10n_py_cdc}.xml"
        if attachment and attachment.mimetype != "application/xml":
            attachment.mimetype = "application/xml"
        return attachment

    def _l10n_py_sifende_prepare_send(self):
        """Deja listos el KuDE y el XML, y descarta un PDF de Odoo guardado antes."""
        self.ensure_one()
        kude = self._l10n_py_sifende_kude_content()
        self._l10n_py_sifende_xml_content()
        pdf = self.sudo().invoice_pdf_report_id
        if pdf and pdf.raw != kude:
            self.sudo().invoice_pdf_report_file = False

    def _is_ready_to_be_sent(self):
        self.ensure_one()
        if self._sifende_delivery_policy()["send_error"]:
            return False
        return super()._is_ready_to_be_sent()

    def _get_report_base_filename(self):
        self.ensure_one()
        if self._sifende_delivery_policy()["pdf_kind"] == "kude":
            return f"KUDE_{self.l10n_py_cdc}"
        return super()._get_report_base_filename()

    def action_send_and_print(self):
        policies = {move: move._sifende_delivery_policy() for move in self}
        errors = [
            policy["send_error"]
            for move, policy in policies.items()
            if move.state == "posted" and policy["send_error"]
        ]
        if errors:
            raise UserError("\n".join(errors))
        if len(self) == 1 and policies[self]["pdf_kind"] == "kude":
            self._l10n_py_sifende_prepare_send()
        return super().action_send_and_print()

    def action_print_pdf(self):
        self.ensure_one()
        if self._sifende_delivery_policy()["pdf_kind"] == "kude":
            self._l10n_py_sifende_kude_content()
        return super().action_print_pdf()

    def _get_invoice_legal_documents(self, filetype, allow_fallback=False):
        self.ensure_one()
        policy = self._sifende_delivery_policy()
        kind = policy["pdf_kind"]
        if filetype == "pdf" and filetype in policy["legal_filetypes"]:
            stored = (
                self.invoice_pdf_report_id
                if kind == "cancellation_notice"
                else self.l10n_py_kude_pdf
            )
            if stored or allow_fallback:
                return self._l10n_py_sifende_legal_document(kind, filetype)
        return super()._get_invoice_legal_documents(
            filetype, allow_fallback=allow_fallback
        )

    def _get_invoice_legal_documents_all(self, allow_fallback=False):
        self.ensure_one()
        policy = self._sifende_delivery_policy()
        kind = policy["pdf_kind"]
        if policy["legal_filetypes"] and (
            allow_fallback
            or (
                self.invoice_pdf_report_id
                if kind == "cancellation_notice"
                else self.l10n_py_kude_pdf and self.l10n_py_edi_xml
            )
        ):
            return [
                self._l10n_py_sifende_legal_document(kind, filetype)
                for filetype in policy["legal_filetypes"]
            ]
        return super()._get_invoice_legal_documents_all(allow_fallback=allow_fallback)

    def _l10n_py_sifende_legal_document(self, pdf_kind, filetype):
        if pdf_kind == "cancellation_notice":
            return self._l10n_py_sifende_cancelled_document()
        if filetype == "pdf":
            return self._l10n_py_sifende_kude_document()
        return {
            "filename": self.l10n_py_edi_xml_filename or f"{self.l10n_py_cdc}.xml",
            "filetype": filetype,
            "content": self._l10n_py_sifende_xml_content(),
        }

    def _l10n_py_sifende_kude_document(self):
        return {
            "filename": f"{self._get_report_base_filename()}.pdf",
            "filetype": "pdf",
            "content": self._l10n_py_sifende_kude_content(),
        }
