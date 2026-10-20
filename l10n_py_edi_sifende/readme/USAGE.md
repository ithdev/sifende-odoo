1. Confirme la factura y pulse **Enviar a EDI**. El documento pasa a *Procesando*.
2. El estado se actualiza solo: *Aceptado* con el XML y el KuDE adjuntos, o *Rechazado* con
   los mensajes de SIFEN en el campo *Mensaje EDI*. Si el webhook está registrado, Sifende
   avisa a Odoo en cuanto SIFEN responde; si no, o si el aviso se pierde, lo hace el cron
   «Sifende: verificar estado de documentos» (cada 5 minutos).
3. Si el documento queda *Rechazado* o con *Error*, corrija el dato y pulse **Reintentar**.
   Un reintento tras una corrección usa una clave de idempotencia nueva. Si el fallo fue de
   conexión, se reintenta con la misma clave y no se duplica el documento.
4. En una nota de crédito o de débito, elija el *Motivo de emisión* en la pestaña
   *Facturación electrónica*. En una nota de remisión, indique también el responsable de la
   emisión y los kilómetros del recorrido.
5. Si SIFEN rechazó un documento y se cambió la fecha o el código de seguridad, Sifende pide
   inutilizar el número y emitir uno nuevo (*Facturación electrónica > Inutilización de
   números*).

La vista previa del XML y del QR no está disponible: Sifende no genera documentos sin
emitirlos.
