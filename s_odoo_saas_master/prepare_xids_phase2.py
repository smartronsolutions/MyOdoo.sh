# -*- coding: utf-8 -*-
"""Pré-mappe les xml_ids Phase 2 sur les enregistrements existants
(saas_odoo_version_19 + configs 19.0) pour éviter les doublons à l'upgrade."""

IMD = self.env['ir.model.data']

# 1. Version 19.0
ver = self.env['saas.odoo.version'].search([('docker_image_tag', '=', '19.0')], limit=1)
if not ver:
    raise Exception('Version 19.0 introuvable')
xid_ver = IMD.search([('module', '=', 's_odoo_saas_master'),
                      ('name', '=', 'saas_odoo_version_19')])
if xid_ver:
    xid_ver.write({'res_id': ver.id, 'model': 'saas.odoo.version', 'noupdate': True})
    print('XID_VER: UPDATED', ver.id)
else:
    IMD.create({'module': 's_odoo_saas_master', 'name': 'saas_odoo_version_19',
                'model': 'saas.odoo.version', 'res_id': ver.id, 'noupdate': True})
    print('XID_VER: CREATED', ver.id)

# 2. Configs 19.0
cfgs = self.env['saas.odoo.version.config'].search([('odoo_version_id', '=', ver.id)])
print('CFGS_TO_MAP:', len(cfgs))
for c in cfgs:
    xname = 'saas_odoo190_config_%s' % c.name
    xid = IMD.search([('module', '=', 's_odoo_saas_master'), ('name', '=', xname)])
    if xid:
        xid.write({'res_id': c.id, 'model': 'saas.odoo.version.config', 'noupdate': True})
    else:
        IMD.create({'module': 's_odoo_saas_master', 'name': xname,
                    'model': 'saas.odoo.version.config', 'res_id': c.id, 'noupdate': True})
print('XID_CFG: DONE')

self.env.cr.commit()
print('COMMITTED')
