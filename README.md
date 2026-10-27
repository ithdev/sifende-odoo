# sifende-odoo

![Odoo 19.0](https://img.shields.io/badge/Odoo-19.0-714B67)
![Madurez: Beta](https://img.shields.io/badge/madurez-Beta-yellow)
![Licencia: LGPL-3](https://img.shields.io/badge/licencia-LGPL--3-blue)

Facturación electrónica de SIFEN en Odoo a través de [Sifende](https://sifende.com.py), para la localización paraguaya
de la OCA ([OCA/l10n-paraguay](https://github.com/OCA/l10n-paraguay)). Esta rama es la **19.0**.

| Módulo | Descripción |
|---|---|
| [l10n_py_edi_sifende](l10n_py_edi_sifende/) | Registra a Sifende como proveedor de `l10n_py_edi_base`: FE, NCE, NDE, NRE y AFE |

Odoo numera las facturas y arma el documento; Sifende lo firma, lo envía a SIFEN y devuelve el resultado y el KuDE. No
hace falta cargar el certificado ni el CSC en Odoo.

## Instalación

### Requisitos

- Odoo 19.0, en Odoo.sh o en un servidor propio. Odoo Online no admite módulos de terceros.
- [OCA/l10n-paraguay](https://github.com/OCA/l10n-paraguay) en la rama 19.0, que trae `l10n_py_edi_base`. El commit
  contra el que se prueba este módulo está en `scripts/fetch-oca.sh`.
- Las dependencias de Python de los dos repos, las mismas que instala el `Dockerfile`.
- Una cuenta de Sifende con el contribuyente, el timbrado y una API key.

### Odoo.sh

En el repo de tu proyecto, agregá los dos repos como submódulos:

```bash
git submodule add -b 19.0 https://github.com/OCA/l10n-paraguay.git l10n-paraguay
git submodule add -b 19.0 https://github.com/ithdev/sifende-odoo.git sifende-odoo
```

Sumá al `requirements.txt` de la raíz las dependencias de Python:

```text
cryptography
num2words
pykude
python-stdnum
qrcode
requests
```

Hacé push, y en la base, andá a **Aplicaciones**, actualizá la lista e instalá
*Paraguay - Facturación electrónica con Sifende*.

### Servidor propio

```bash
git clone -b 19.0 https://github.com/OCA/l10n-paraguay.git /opt/odoo/addons/l10n-paraguay
git clone -b 19.0 https://github.com/ithdev/sifende-odoo.git /opt/odoo/addons/sifende-odoo
pip install cryptography num2words pykude python-stdnum qrcode requests
```

Agregá las dos carpetas al `addons_path` de `odoo.conf`, reiniciá Odoo e instalá el módulo desde **Aplicaciones**, o con
`odoo -d <base> -i l10n_py_edi_sifende`.

### Configuración y uso

El conector, la API key, el webhook y los permisos se configuran como explica la sección
[Configuration](l10n_py_edi_sifende/README.rst#configuration) del módulo; el envío, los reintentos, la cancelación y la
inutilización, en [Usage](l10n_py_edi_sifende/README.rst#usage).

## Desarrollo

Requiere Docker. El entorno monta `l10n_py_edi_sifende` y las addons de OCA pineadas al commit de
`scripts/fetch-oca.sh` (se clonan en `vendor/`, ignorado por git). Los puertos de Postgres y Odoo están en
`docker-compose.yml`; `SIFENDE_ODOO_PG_PORT` y `SIFENDE_ODOO_PORT` los cambian.

```bash
make help        # todos los comandos, con el puerto de Odoo
make test        # instala el módulo en una base nueva y corre sus tests
make run         # levanta Odoo
make down        # borra contenedores y volúmenes
```

`make test` equivale a:

```bash
odoo -d <db> -i l10n_py_edi_sifende --test-enable --test-tags /l10n_py_edi_sifende --stop-after-init
```

Los tests mockean `requests`: no necesitan una API key ni salen a Internet.

## Prueba de punta a punta

Con una API key SANDBOX de Sifende (desde el panel: **API keys**) de un contribuyente con timbrado,
establecimiento 001 y punto de expedición 001 cargados:

```bash
export SIFENDE_E2E_API_KEY=sk_...      # clave SANDBOX
export SIFENDE_E2E_RUC=80012345        # RUC del contribuyente, sin DV
make e2e-reset e2e-setup e2e-cases     # base nueva, empresa, timbrado, conector, casos
make e2e-run                           # Odoo sobre la base de prueba (admin / admin)
```

El conector apunta a `https://api.sifende.com.py` (`SIFENDE_E2E_BASE_URL` lo cambia). Los casos
imprimen su evidencia como `E2E[...]`. Los números parten de 1: si ya hay documentos SANDBOX en
ese punto, fijá `SIFENDE_E2E_FIRST_NUMBER` antes de `make e2e-setup`. SANDBOX siempre aprueba y no
admite cancelación ni inutilización: esos caminos los cubren los tests del módulo.

## Soporte

Escribinos a [soporte@sifende.com.py](mailto:soporte@sifende.com.py). La documentación de la API está en
<https://sifende.com.py/docs>.

## Contribuir

Antes de abrir un PR, corré `pre-commit run --all-files` y `make test`. El `README.rst` del módulo se genera desde
`l10n_py_edi_sifende/readme/` con `oca-gen-addon-readme` (lo corre pre-commit): editá los fragmentos, no el `.rst`.

## Licencia

[LGPL-3](LICENSE).
