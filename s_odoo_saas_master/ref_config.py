# -*- coding: utf-8 -*-
inst = self.env['saas.odoo.instance'].browse(114)
print('REF_INST:', inst.id, inst.technical_name, '| version=', inst.odoo_version_id.name if inst.odoo_version_id else None)
print('REF_CONFIG_COUNT:', len(inst.config_ids))
for c in inst.config_ids.sorted('name'):
    print('CFG:', c.name, '=', c.value, '| section=', c.section_id.name)
print('--- VERSION 19 CONFIGS ---')
v = self.env['saas.odoo.version'].search([('docker_image_tag', '=', '19.0')], limit=1)
print('V19:', v.id, v.name, '| configs=', len(v.config_ids))
for c in v.config_ids.sorted('name'):
    print('VCFG:', c.name, '=', c.value, '| section=', c.section_id.name)
