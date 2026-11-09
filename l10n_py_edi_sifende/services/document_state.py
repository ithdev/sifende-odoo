"""States of an electronic document as Sifende reports them.

Pure: no ORM and no HTTP, so it is tested and reasoned about without Odoo.
"""

from dataclasses import dataclass, field

APPROVED_STATES = ("APROBADO", "APROBADO_OBSERVACION")
# the states in which Sifende is (re)trying the submission
RESUBMITTED_STATES = ("PENDIENTE", "EN_LOTE", "ENVIADO")
# webhook event type -> the state it announces
WEBHOOK_STATES = {
    "documento.aprobado": "APROBADO",
    "documento.rechazado": "RECHAZADO",
    "documento.cancelado": "CANCELADO",
}


class StatusUnavailable(Exception):
    """The status query did not return a fiscal state.

    Transport failure, non-2xx answer, a body that is not an object or has no
    `estado`. It is not a state: nothing may be applied to a document because of
    it, and each caller decides how to retry.
    """


@dataclass(frozen=True)
class DocumentState:
    """The answer of `GET /documento-electronico/status/<cdc>`."""

    cdc: str
    estado: str  # as Sifende says it: APROBADO, RECHAZADO, CANCELADO, ERROR...
    message: str | None  # `mensajeRechazo`
    raw: dict = field(compare=False, repr=False)  # the whole body, copied

    @classmethod
    def from_body(cls, cdc, body):
        """Return the state a body carries, or None if it carries none."""
        if not isinstance(body, dict) or not body.get("estado"):
            return None
        return cls(cdc, body["estado"], body.get("mensajeRechazo"), dict(body))

    @property
    def approved(self):
        return self.estado in APPROVED_STATES
