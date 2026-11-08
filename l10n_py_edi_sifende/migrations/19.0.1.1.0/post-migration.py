def migrate(cr, version):
    """Asigna empresa a los logs EDI anteriores al aislamiento por empresa.

    Se usa, en orden, la empresa de la factura, la de la factura con el mismo
    CDC y, para los logs de Sifende, la del único conector Sifende si hay uno
    solo. El resto queda sin empresa: solo lo ve el administrador del sistema.
    """
    cr.execute(
        """
        UPDATE l10n_py_edi_log log
           SET company_id = move.company_id
          FROM account_move move
         WHERE log.company_id IS NULL
           AND log.document_id = move.id
        """
    )
    cr.execute(
        """
        UPDATE l10n_py_edi_log log
           SET company_id = by_cdc.company_id
          FROM (
                SELECT l10n_py_cdc, MIN(company_id) AS company_id
                  FROM account_move
                 WHERE l10n_py_cdc IS NOT NULL
                 GROUP BY l10n_py_cdc
                HAVING COUNT(DISTINCT company_id) = 1
               ) by_cdc
         WHERE log.company_id IS NULL
           AND log.cdc = by_cdc.l10n_py_cdc
        """
    )
    cr.execute(
        """
        SELECT MIN(company_id)
          FROM l10n_py_edi_connector
         WHERE provider_type = 'sifende'
        HAVING COUNT(DISTINCT company_id) = 1
        """
    )
    row = cr.fetchone()
    if row:
        cr.execute(
            """
            UPDATE l10n_py_edi_log
               SET company_id = %s
             WHERE company_id IS NULL
               AND provider = 'sifende'
            """,
            (row[0],),
        )
