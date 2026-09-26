# -*- coding: utf-8 -*-
servers = self.env['saas.odoo.server'].search([])
for s in servers:
    print('SERVER:', s.id, s.name, '| pserver:', s.pserver_id.id, s.pserver_id.name)
    for a in s.extra_addon_ids:
        print('  ADDON:', a.id, repr(a.source_path))
