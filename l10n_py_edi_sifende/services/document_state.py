"""States of an electronic document as Sifende reports them.

Pure: no ORM and no HTTP, so it is tested and reasoned about without Odoo.
"""

APPROVED_STATES = ("APROBADO", "APROBADO_OBSERVACION")
# the states in which Sifende is (re)trying the submission
RESUBMITTED_STATES = ("PENDIENTE", "EN_LOTE", "ENVIADO")
# webhook event type -> the state it announces
WEBHOOK_STATES = {
    "documento.aprobado": "APROBADO",
    "documento.rechazado": "RECHAZADO",
    "documento.cancelado": "CANCELADO",
}
