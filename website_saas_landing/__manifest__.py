{
    'name': 'Website SaaS Landing Page',
    'version': '18.0.9.1.0',
    'category': 'Website',
    'summary': 'Premium SaaS landing page, set as the website home page (/) - Odoo 18 compatible',
    'description': '''
        A professional, fully-responsive SaaS cloud landing page module for Odoo 18.

        Features:
        - Modern, responsive cloud-platform design
        - Hero section with CTA buttons
        - Value props, deployment pipeline, architecture and monitoring diagrams
        - Application groups, planning section and FAQ accordion
        - 2-tier pricing plans
        - Professional footer
        - Fully scoped CSS (zero global impact)

        The landing page becomes the website home page: installing the module
        switches the ``/`` URL to this landing page (Website > Configuration >
        Website setting ``homepage_url``). It is also accessible at /saas.

        Customization:
        - Easy color customization via CSS variables
        - Editable content sections
        - Responsive design works on all devices
        - No external dependencies required
    ''',
    'author': 'Your Company',
    'website': 'https://yourdomain.com',
    'license': 'LGPL-3',
    'depends': [
        'auth_signup',
        'crm',
        'mail',
        'website',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/website_saas_enquiry_security.xml',
        'data/ir_sequence_data.xml',
        'data/ir_config_parameter_data.xml',
        'data/mail_template_data.xml',
        'views/website_saas_enquiry_views.xml',
        'views/crm_lead_views.xml',
        'views/res_config_settings_views.xml',
        'views/saas_header.xml',
        'views/auth_pages.xml',
        'views/saas_shared_templates.xml',
        'views/saas_landing_template.xml',
        'views/saas_about_page.xml',
        'views/saas_services_page.xml',
        'views/saas_contact_page.xml',
        'views/saas_legal_pages.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'website_saas_landing/static/src/css/saas_landing.css',
            'website_saas_landing/static/src/css/saas_home.css',
            'website_saas_landing/static/src/js/saas_landing.js',
        ],
    },
    'images': [
        'static/description/icon.png',
        'static/description/thumbnail.png',
    ],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'installable': True,
    'application': False,
    'auto_install': False,
}
