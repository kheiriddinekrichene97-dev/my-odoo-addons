{
    "name": "My Module",
    "version": "1.1",
    "depends": ["base", "web"],
    "data": [
        "views/partner_views.xml",
        "views/home_menu_actions.xml",
        "views/login_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "my_module/static/src/**/*",
        ],
        "web.assets_frontend": [
            "my_module/static/login/login.scss",
        ],
    },
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
