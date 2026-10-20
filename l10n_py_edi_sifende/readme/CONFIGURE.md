1. En Sifende, cree una API key del ambiente que corresponda (Sandbox, Pruebas o Producción).
2. En *Facturación electrónica > Conectores*, cree un conector con proveedor **Sifende**.
   Solo se admite un conector por empresa.
3. Elija el ambiente del conector (*Pruebas* o *Producción*), pegue la API key y, si usa un
   entorno propio de Sifende, cambie la URL base. La API key solo la ven los administradores.
4. Pulse **Probar Conexión**. Falla si el ambiente del conector no coincide con el de la API
   key, y advierte si el timbrado, el establecimiento o el punto de expedición de Odoo no
   existen en Sifende.
5. Cargue en Sifende el mismo timbrado que usa Odoo. Se recomienda un punto de expedición
   exclusivo para Odoo: Sifende acepta el número de documento que manda Odoo y rechaza los
   que ya están en uso.
6. Opcional: en *Inventario > Configuración > Unidades de medida*, ajuste el *Código de
   unidad de medida SIFEN* de cada unidad (por defecto 77, unidad).
