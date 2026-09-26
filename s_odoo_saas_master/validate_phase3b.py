# -*- coding: utf-8 -*-
module = self.env['ir.module.module'].search([('name', '=', 's_odoo_saas_master')], limit=1)
print('MODULE_VERSION:', module.installed_version)

img = self.env['saas.docker.image'].browse(8)
print('IMG8:', img.image_name)
res = img.action_detect_requirements()
print('DETECT8:', res['params']['message'])
print('--- AUTO_REQS8 (après fix parsing) ---')
for l in (img.auto_requirements or '').splitlines():
    print('R:', l)

# Exclusion du fichier pandas incompatible sur les 3 images custom
imgs = self.env['saas.docker.image'].search([('build_status', '!=', 'not_built')])
for im in imgs:
    im.write({'ignored_requirements_files': '*ks_dashboard_ninja*'})
    print('IGNORE_SET:', im.id, im.image_name)

# Re-détection avec exclusion
res2 = img.action_detect_requirements()
print('DETECT8_AFTER_IGNORE:', res2['params']['message'])
for l in (img.auto_requirements or '').splitlines():
    print('R2:', l)
