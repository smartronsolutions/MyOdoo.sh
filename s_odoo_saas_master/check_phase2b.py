# -*- coding: utf-8 -*-
v19 = self.env['saas.odoo.version'].search([('docker_image_tag', '=', '19.0')], limit=1)
cfgs = v19.config_ids
print('CFGS:', len(cfgs))
for c in cfgs[:40]:
    xmlids = self.env['ir.model.data'].search([('model', '=', 'saas.odoo.version.config'),
                                                ('res_id', '=', c.id)])
    print('CFG:', c.id, c.name, '=', c.value, '| xmlids:', [(x.module, x.name) for x in xmlids])
