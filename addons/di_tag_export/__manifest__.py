{
    'name': 'Tag Print (Cartellino)',
    'version': '19.0.4.7.0',
    'summary': 'Print product price tags (cartellini) as PDF labels for thermal printers',
    'description': """
Tag Print (Cartellino)
======================
Adds a "Cartellino" PDF report to products. Select one or more variants in the
Product Variants list, use Print > Cartellino, and get one print-ready label per
variant (70 x 38 mm) reproducing the Dimensione Immagine price tag, ready to send
straight to the thermal printer.

Each label is computed from existing Odoo data (no database fields are added):
- internal reference, colour, description, size, scannable Code128 barcode
- sales price, discount %% and the price the tills charge (the Promo
  pricelist, or Dettaglio where no Promo pricelist is configured)
- Marca, Stagione, Reparto, Posizionamento
- the wholesale (Ingrosso) code and the "buono acquisto" code

Labels always come off the printer in one order, whichever screen prints them
(models/tag_order.py): one pile per source warehouse - with a blank card
naming it when a print holds several - and inside each pile ABBI SCA,
ACCE / BORS, ABBI APP, ACCE SCA, or Donna then Uomo at MAG - CENTRALE
PISTUNINA, ascending barcode inside each step.
""",
    'author': 'Dimensione Immagine',
    'category': 'Inventory/Inventory',
    'depends': ['product'],
    'data': [
        'report/report_product_tag.xml',
        'report/report_product_tag_templates.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
