# -*- coding: utf-8 -*-
imgs = self.env['saas.docker.image'].search([])
for im in imgs:
    print('IMG:', im.id, im.image_name, im.build_status)

insts = self.env['saas.odoo.instance'].search([('docker_image_id', '!=', False)])
for i in insts:
    print('INST_IMG:', i.id, i.technical_name, i.state, '->', i.docker_image_id.image_name)
