{
    'name': 'Website SaaS Landing Page',
    'version': '18.0.1.0.1',
    'category': 'Website',
    'summary': 'Premium SaaS landing page at /saas - Odoo 18 compatible',
    'description': '''
        A professional, fully-responsive SaaS landing page module for Odoo 18.
        
        Features:
        - Modern, responsive design
        - Hero section with CTA buttons
        - Feature showcase with icons
        - How It Works section with timeline
        - 3-tier pricing plans
        - Customer testimonials
        - Professional footer
        - Smooth animations and interactions
        - Fully scoped CSS (zero global impact)
        - Optimized for performance
        
        The landing page is accessible at /saas on your website.
        
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
        'website',
    ],
    'data': [
        'views/saas_header.xml',
        'views/auth_pages.xml',
        'views/saas_shared_templates.xml',
        'views/saas_landing_template.xml',
        'views/saas_about_page.xml',
        'views/saas_services_page.xml',
        'views/saas_contact_page.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'website_saas_landing/static/src/css/saas_landing.css',
            'website_saas_landing/static/src/js/saas_landing.js',
        ],
    },
    'images': [
        'static/description/icon.png',
        'static/description/thumbnail.png',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
