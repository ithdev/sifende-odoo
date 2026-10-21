1. En Sifende, cree una API key del ambiente que corresponda (Sandbox, Pruebas o Producción).
2. En *Facturación electrónica > Conectores*, cree un conector con proveedor **Sifende**.
   Solo se admite un conector por empresa.
3. Elija el ambiente del conector (*Pruebas* o *Producción*), pegue la API key y, si usa un
   entorno propio de Sifende, cambie la URL base (HTTPS; HTTP solo se admite para
   `localhost`). La API key y la URL base solo las ven y editan los administradores
   (*Ajustes*): la URL decide adónde viaja la clave.
4. Pulse **Probar Conexión**. Falla si el ambiente del conector no coincide con el de la API
   key (`sk_live_` es Producción; `sk_sandbox_` y `sk_test_`, Pruebas; esa coincidencia también se
   valida al guardar el conector y antes de enviar, cancelar o inutilizar), y advierte si el timbrado, el establecimiento o el punto de expedición de Odoo no
   existen en Sifende.
5. Cargue en Sifende el mismo timbrado que usa Odoo. Se recomienda un punto de expedición
   exclusivo para Odoo: Sifende acepta el número de documento que manda Odoo y rechaza los
   que ya están en uso.
6. Opcional: en *Inventario > Configuración > Unidades de medida*, ajuste el *Código de
   unidad de medida SIFEN* de cada unidad (por defecto 77, unidad).
7. Opcional pero recomendado: pulse **Registrar webhook** para que Sifende avise a Odoo apenas
   SIFEN responde, sin esperar al cron. Odoo debe ser accesible desde Internet por HTTPS y
   su URL base (`web.base.url`, en *Ajustes > Técnico > Parámetros del sistema*) debe ser la
   pública: Sifende rechaza `http` y las direcciones locales. El botón registra
   `<web.base.url>/l10n_py_edi_sifende/webhook/<id del conector>` para los eventos
   `documento.aprobado`, `documento.rechazado` y `documento.cancelado`, y guarda el secreto
   de firma, que solo ven los administradores. Si la URL ya estaba registrada en Sifende, el
   botón lo informa y hay que pulsar **Rotar secreto** para obtener un secreto. Si el webhook
   ya no existe en Sifende, **Rotar secreto** lo olvida y avisa: pulse **Registrar webhook**. **Quitar
   webhook** lo elimina en Sifende. Cada aviso solo dispara una consulta del estado vigente en
   Sifende, así que un aviso atrasado no pisa el estado real. El cron sigue activo como respaldo.
8. Con varias bases de datos en el mismo servidor, la URL pública debe resolver a la base
   correcta (`dbfilter` o el subdominio), porque Sifende no envía el nombre de la base.
9. Permisos. Cada conector solo se opera desde su empresa. Enviar, reintentar y cancelar un
   documento exige el rol *Contabilidad > Facturación* y poder editar el documento; consultar
   su estado, también el de solo lectura. *Inutilizar números* y **Probar Conexión** son del
   *Contabilidad > Administrador*. La API key, la URL base y el webhook son de *Ajustes*.
