1. Confirme la factura y pulse **Enviar a EDI**. El documento pasa a *Procesando*.
2. El estado se actualiza solo: *Aceptado* con el XML y el KuDE adjuntos, o *Rechazado* con
   los mensajes de SIFEN en el campo *Mensaje EDI*. Si el webhook está registrado, Sifende
   avisa a Odoo en cuanto SIFEN responde; si no, o si el aviso se pierde, lo hace el cron
   «Sifende: verificar estado de documentos» (cada 5 minutos).
3. Si el documento queda con *Error*, o *Rechazado* antes de llegar a SIFEN (por ejemplo por un
   campo obligatorio), corrija el dato y pulse **Reintentar**. Un reintento tras una corrección
   usa una clave de idempotencia nueva. Si el fallo fue de conexión, se reintenta con la misma
   clave y no se duplica el documento.
4. En una nota de crédito o de débito, elija el *Motivo de emisión* en la pestaña
   *Facturación electrónica*. En una nota de remisión, indique también el responsable de la
   emisión y los kilómetros del recorrido.
5. Si una factura al contado se paga con *Cheque* (*Tipo de Pago* en la pestaña
   *Facturación electrónica*), complete el número del cheque (solo dígitos, hasta 8) y el banco
   (de 4 a 20 caracteres): Sifende los exige.
6. Si SIFEN rechaza un documento ya enviado (*Rechazado* con el CDC cargado), Sifende no
   permite reenviar ese número ni con los mismos datos, y **Reintentar** muestra el
   procedimiento en lugar de enviar. Con el rol *Contabilidad > Administrador*, abra
   *Facturación Electrónica > Inutilización de números* (dentro de la aplicación de
   Facturación o Contabilidad), cree la inutilización con el timbrado y el número del
   documento rechazado, y pulse **Enviar al SIFEN**. Luego pulse **Restablecer a borrador**
   y **Cancelar** en el documento rechazado y emita uno nuevo, que recibe el número siguiente.
   Odoo solo permite inutilizar números sin documento o de documentos rechazados.
7. Para cancelar un documento aceptado, pulse **Cancelar EDI**: se abre el asistente
   «Cancelar documento electrónico», donde se escribe el motivo (se envía a SIFEN) y se
   confirma con **Cancelar Documento**. Hace falta el rol *Contabilidad > Facturación* y
   estar dentro del plazo que valida Odoo (48 horas las facturas y autofacturas, 7 días las
   notas).

La vista previa del XML y del QR no está disponible: Sifende no genera documentos sin
emitirlos.
