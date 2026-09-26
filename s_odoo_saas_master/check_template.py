# -*- coding: utf-8 -*-
inst = self.env['saas.odoo.instance'].browse(20)
print('INST20:', inst.id, inst.technical_name, '| is_template=', inst.is_template,
      '| state=', inst.state, '| op=', inst.operation_state)
templates = self.env['saas.odoo.instance'].search([('template_instance_id', '=', inst.id)])
print('USED_AS_TEMPLATE_BY:', [(t.id, t.technical_name, t.state) for t in templates])
print('TAGS20:', [(t.id, t.name) for t in inst.template_tag])
