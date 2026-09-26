{
    'name': 'SaaS Storage Management',
    'version': '18.0.1.1.0',
    'category': 'SaaS',
    'summary': 'Storage management for SaaS instances - Admin manages, Clients view in portal',
    'author': 'SkyERP',
    'depends': ['s_odoo_saas_master', 'mail', 'website'],
    'data': [
        'security/ir_model_access.xml',
        'data/storage_mail_template_data.xml',
        'views/saas_instance_storage_inherit.xml',
        'views/storage_views.xml',
        'views/portal_storage_views.xml',
        'data/ir_cron_storage_check.xml',
    ],
    'installable': True,
    'auto_install': True,
    'license': 'LGPL-3',
}
