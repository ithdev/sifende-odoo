# sifende-odoo

Conector de [Sifende](https://sifende.com.py) para la localización paraguaya de la OCA
([OCA/l10n-paraguay](https://github.com/OCA/l10n-paraguay)). Esta rama es la **19.0**.

| Módulo | Descripción |
|---|---|
| [l10n_py_edi_sifende](l10n_py_edi_sifende/) | Registra a Sifende como proveedor de `l10n_py_edi_base`: FE, NCE, NDE, NRE y AFE |

Licencia LGPL-3.

## Desarrollo

Requiere Docker. El entorno monta `l10n_py_edi_sifende` y las addons de OCA pineadas al commit
`c51fe3d` (se clonan en `vendor/`, ignorado por git). Postgres escucha en `5442` y Odoo en `8179`
(`SIFENDE_ODOO_PG_PORT` y `SIFENDE_ODOO_PORT` los cambian).

```bash
make test        # instala el módulo en una base nueva y corre sus tests
make run         # Odoo en http://localhost:8179
make down        # borra contenedores y volúmenes
```

`make test` equivale a:

```bash
odoo -d <db> -i l10n_py_edi_sifende --test-enable --test-tags /l10n_py_edi_sifende --stop-after-init
```

Los tests mockean `requests`: no necesitan una API key ni salen a Internet.

Para correr el linter: `pre-commit run --all-files`.

## Referencia

- API de Sifende: <https://sifende.com.py/docs>
