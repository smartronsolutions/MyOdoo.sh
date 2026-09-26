# -*- coding: utf-8 -*-
# ÉTAT DES LIEUX EN LECTURE SEULE — ne modifie rien
print('=== INSTANCE ODOOSOSI01 ===')
inst = self.env['saas.odoo.instance'].search([('technical_name', 'ilike', 'odoososi01')])
for i in inst:
    print('INST:', i.id, i.name, '|', i.technical_name, '| state=', i.state, i.operation_state)
    print('  server:', i.odoo_server_id.name if i.odoo_server_id else None)
    print('  version:', i.odoo_server_id.odoo_version_id.name if i.odoo_server_id and i.odoo_server_id.odoo_version_id else None)
    print('  docker_image_id:', i.docker_image_id.id if i.docker_image_id else None,
          i.docker_image_id.image_name if i.docker_image_id else None)
    print('  docker_odoo_image:', i.docker_odoo_image)
    print('  custom_addons:', [(c.name, c.clone_uri, c.branch) for c in i.custom_addon_ids])
    print('  installed_apps:', [(a.name) for a in i.installed_app_ids][:20])
    print('  default_module:', i.default_module)

print()
print('=== IMAGES DOCKER ===')
for img in self.env['saas.docker.image'].search([], order='id'):
    reqs = (img.python_requirements or '').replace('\n', ' | ')
    print('IMG:', img.id, img.image_name, '| tag=', img.tag_id.name,
          '| status=', img.build_status, '| servers=', len(img.server_ids))
    print('  REQS:', reqs[:200])
    print('  SYS:', (img.system_packages or '').replace('\n', ' | ')[:200])

print()
print('=== MODULE EN BASE ===')
mod = self.env['ir.module.module'].search([('name', '=', 's_odoo_saas_master')], limit=1)
print('MODULE:', mod.state, mod.installed_version)
