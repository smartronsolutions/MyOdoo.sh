# -*- coding: utf-8 -*-
ver = self.env['saas.odoo.version'].search([('docker_image_tag', '=', '19.0')], limit=1)
print('VER19:', ver.id, ver.name)
cfg = self.env['saas.odoo.version.config'].search([('odoo_version_id', '=', ver.id)], order='name')
print('CFG_COUNT:', len(cfg))
for c in cfg:
    print('CFG:', c.name, '|', c.value, '|', c.section_id.name)
# xml_id de la version 19 ?
md = self.env['ir.model.data'].search([('model', '=', 'saas.odoo.version'), ('res_id', '=', ver.id)])
print('XID:', [(m.module, m.name) for m in md])
