# -*- coding: utf-8 -*-
v19 = self.env['saas.odoo.version'].search([('docker_image_tag', '=', '19.0')], limit=1)
print('V19_ID:', v19.id, '| configs:', len(v19.config_ids))
# xml_id existant ?
try:
    rec = self.env.ref('s_odoo_saas_master.saas_odoo_version_19', raise_if_not_found=True)
    print('XMLID_V19:', rec.id, rec.name)
except Exception as e:
    print('XMLID_V19: ABSENT')
# xml_ids des configs 19.0
xmlids = self.env['ir.model.data'].search([('model', '=', 'saas.odoo.version.config'),
                                            ('res_id', 'in', v19.config_ids.ids)])
print('XMLID_CFG_COUNT:', len(xmlids), [x.name for x in xmlids][:5])
# section options id
sec = self.env.ref('s_odoo_saas_master.saas_odoo_version_config_section_options')
print('SECTION_OPTIONS_ID:', sec.id, sec.name)
# une instance deployée pour tester le dry-run
insts = self.env['saas.odoo.instance'].search([('state', '=', 'deploy')], limit=3)
print('DEPLOYED:', [(i.id, i.technical_name) for i in insts])
