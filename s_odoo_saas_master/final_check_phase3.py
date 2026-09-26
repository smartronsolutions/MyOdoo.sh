# -*- coding: utf-8 -*-
module = self.env['ir.module.module'].search([('name', '=', 's_odoo_saas_master')], limit=1)
print('MODULE_VERSION:', module.installed_version)
imgs = self.env['saas.docker.image'].search([])
for im in imgs:
    auto = (im.auto_requirements or '').splitlines()
    print('IMG:', im.id, im.image_name, '| status:', im.build_status,
          '| ignored:', repr(im.ignored_requirements_files),
          '| auto_count:', len(auto))
    print('  AUTO:', ', '.join(auto) if auto else '(aucun)')
