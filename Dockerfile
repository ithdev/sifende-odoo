FROM odoo:18

USER root
# Dependencias externas de l10n_py_* (manifests de OCA/l10n-paraguay) y del conector
RUN pip install --no-cache-dir --break-system-packages \
    pykude \
    qrcode \
    python-stdnum \
    num2words \
    cryptography \
    requests

USER odoo
