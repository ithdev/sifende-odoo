Conector de [Sifende](https://sifende.com.py) para la facturación electrónica de Paraguay.

Registra a Sifende como proveedor dentro de `l10n_py_edi_base`. Odoo sigue numerando
las facturas y armando los datos del documento; Sifende firma el XML, lo envía a SIFEN
y devuelve el resultado y el KuDE. No hace falta cargar certificado, CSC ni la librería
`sifen` en Odoo.

Documentos admitidos: factura (FE), nota de crédito (NCE), nota de débito (NDE), nota de
remisión (NRE) y autofactura (AFE).

El envío es asincrónico: el documento queda «Procesando» con su CDC y su QR, y pasa a
«Aceptado», con el XML firmado y el KuDE adjuntos, cuando SIFEN lo aprueba.
