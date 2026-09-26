# -*- coding: utf-8 -*-
imgs = self.env['saas.docker.image'].search([('image_name', '!=', 'odoo:19.0')])
for im in imgs:
    im.write({'ignored_requirements_files': '*ks_dashboard_ninja*'})
    print('IGNORE_SET:', im.id, im.image_name, '->', repr(im.ignored_requirements_files))
self.env.cr.commit()
print('COMMITTED')
