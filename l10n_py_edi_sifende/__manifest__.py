{
    "name": "Paraguay - Facturación electrónica con Sifende",
    "version": "19.0.1.0.0",
    "category": "Accounting/Localizations/EDI",
    "summary": "Conector de Sifende para la facturación electrónica de Paraguay",
    "author": "ITH, Odoo Community Association (OCA)",
    "website": "https://github.com/OCA/l10n-paraguay",
    "license": "LGPL-3",
    "depends": ["l10n_py_edi_base"],
    "external_dependencies": {"python": ["requests"]},
    "data": [
        "security/ir.model.access.csv",
        "views/edi_connector_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
