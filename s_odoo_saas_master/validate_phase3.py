# -*- coding: utf-8 -*-
module = self.env['ir.module.module'].search([('name', '=', 's_odoo_saas_master')], limit=1)
print('MODULE_VERSION:', module.installed_version)

img = self.env['saas.docker.image'].browse(8)
print('IMG:', img.id, img.image_name, '| servers:', [(s.id, s.name) for s in img.server_ids])
res = img.action_detect_requirements()
print('DETECT_RESULT:', res['params']['message'])
print('AUTO_REQS:')
print(img.auto_requirements or '(vide)')
print('AUTO_REQS_FILES_DETAILS: see build? no build yet')

# Test Dockerfile variants
manual = ['qifparse', 'reportlab']
detected = ['pandas==2.0.3', 'xlrd==2.0.1', 'woocommerce==2.1.1']
merged = img._merge_requirements(manual, detected)
print('MERGED:', merged)
print('--- DOCKERFILE FILE-MODE ---')
print(img._generate_dockerfile('odoo:19.0', ['libcairo2'], merged, use_requirements_file=True))
print('--- DOCKERFILE INLINE-MODE ---')
print(img._generate_dockerfile('odoo:19.0', ['libcairo2'], merged, use_requirements_file=False))
print('--- FILTER TEST ---')
for t in ['./addons/x.whl; marker', '../x', '/abs', '-r other.txt', '--constraint c.txt', 'pandas==2.0.3', '# comment']:
    print(repr(t), '->', repr(img._is_safe_requirement_line(t)))
