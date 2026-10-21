1. Confirme la factura y pulse **Enviar a EDI** (solo se envían documentos publicados). El
   documento pasa a *Procesando*. Una vez enviado, el tipo de pago, el cheque, la tarjeta, la
   condición de pago y los vencimientos ya no se pueden modificar.
2. El estado se actualiza solo: *Aceptado* con el XML y el KuDE adjuntos, o *Rechazado* con
   los mensajes de SIFEN en el campo *Mensaje EDI*. Si el webhook está registrado, Sifende
   avisa a Odoo en cuanto SIFEN responde; si no, o si el aviso se pierde, lo hace el cron
   «Sifende: verificar estado de documentos» (cada 5 minutos).
3. Si el documento queda con *Rechazado* antes de llegar a SIFEN (por ejemplo por un campo
   obligatorio), corrija el dato y pulse **Reintentar**: usa una clave de idempotencia nueva.
   Si queda con *Error* por un fallo de conexión o de Sifende antes de registrar el documento
   (el mensaje lo indica), **Reintentar** usa la misma clave y no se duplica.
   Si el *Error* es un fallo definitivo del envío a SIFEN, el documento ya tiene CDC y número
   tomados: **Reintentar** no lo reemite y muestra el procedimiento. Reintente el lote desde el
   panel de Sifende o pídalo a soporte; Odoo consulta el documento cada hora y lo actualiza solo
   (*Procesando* y luego *Aceptado*) cuando vuelve a procesarse con el mismo CDC.
4. En una nota de crédito o de débito, elija el *Motivo de emisión* en la pestaña
   *Facturación electrónica*. El documento asociado se completa solo con el CDC de la factura
   de origen cuando esta ya lo tiene. En una nota de remisión, indique también el responsable de la
   emisión y los kilómetros del recorrido.
5. Si una factura al contado se paga con *Cheque* (*Tipo de Pago* en la pestaña
   *Facturación electrónica*), complete el número del cheque (solo dígitos, hasta 8) y el banco
   (de 4 a 20 caracteres): Sifende los exige. Con tarjeta, elija la *Marca de la tarjeta*.
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

8. En guaraníes, Sifende redondea el total hacia abajo a múltiplos de 50 y exige que los pagos
   y las cuotas lo cubran: el conector reparte los montos de Odoo en proporción y la última
   cuota absorbe el resto.
9. Si Sifende responde que el número ya está en uso, el documento queda en *Error*. Suba
   *Número Desde* del timbrado al siguiente número libre del punto (panel de Sifende >
   Numeración) y pulse **Renumerar**: el documento toma el siguiente número libre, nunca uno que
   Sifende ya informó, y se envía de nuevo con **Enviar a EDI**.

La vista previa del XML y del QR no está disponible: Sifende no genera documentos sin
emitirlos.
