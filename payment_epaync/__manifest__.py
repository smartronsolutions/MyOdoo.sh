{
    'name': 'EpayNC Payment Provider',
    'version': '18.0.1.0.0',
    'category': 'Accounting/Payment Providers',
    'summary': 'Payment Provider: EpayNC (New Caledonia / XPF)',
    'description': """
EpayNC Payment Provider
=======================
Integrates the EpayNC hosted payment gateway (Lyra Networks) for Odoo 18.

Features:
- Hosted Payment Page (form POST redirect)
- HMAC SHA-256 signature verification
- IPN / Webhook server-to-server notification
- Test and Production modes
- Full transaction logging (Accounting > Configuration > EpayNC Logs)
- Website Checkout, Sales Orders, Invoices, Payment Links support
- Multi-company / Multi-website
    """,
    'author': 'SOSI NC',
    'website': 'https://www.sosi-nc.com',
    'depends': [
        'payment',
        'account',
        'website',
        'website_sale',
        'sale_management',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'templates/redirect_form.xml',       # must load BEFORE payment_method_data.xml
        'data/payment_method_data.xml',      # references payment_epaync.redirect_form
        'views/payment_epaync_log_views.xml',
        'views/payment_provider_views.xml',
        'views/payment_transaction_views.xml',
    ],
    'assets': {
        'web.assets_frontend': [],
    },
    'images': ['static/description/icon.png'],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
    'application': False,
}
