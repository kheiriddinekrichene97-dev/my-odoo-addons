{
    "name": "My Module",
    "version": "1.0",
    "depends": ["base", "web"],
    "data": [
        "views/partner_views.xml",
        "views/home_menu_actions.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "my_module/static/src/**/*",
        ],
    },
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
