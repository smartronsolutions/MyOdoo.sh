# -*- coding: utf-8 -*-
for img in self.env['saas.docker.image'].search([('image_name', 'in', ['odoo:19.0-btp', 'odoo:19.0-btp-ocr'])], order='id'):
    print('IMG:', img.id, img.name, '|', img.image_name, '| status=', img.build_status)
    print('  system_packages:', repr(img.system_packages))
    print('  python_requirements:', repr(img.python_requirements))
    print('  dockerfile len:', len(img.dockerfile_content or ''))
