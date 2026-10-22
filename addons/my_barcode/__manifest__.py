{
    "name": "Barcode Scanner",
    "version": "1.0",
    "summary": "Scan products for lookup, inventory counts, receipts and deliveries",
    "category": "Inventory/Inventory",
    "depends": ["stock", "barcodes", "web"],
    "data": [
        "views/barcode_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "my_barcode/static/src/**/*",
        ],
    },
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
