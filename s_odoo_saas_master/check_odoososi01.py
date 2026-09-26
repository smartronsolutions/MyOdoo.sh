# -*- coding: utf-8 -*-
inst = self.env['saas.odoo.instance'].browse(20)
print('INST:', inst.id, inst.technical_name, '| state=', inst.state, inst.operation_state)
print('docker_image_id:', inst.docker_image_id.id if inst.docker_image_id else None)
print('docker_odoo_image:', inst.docker_odoo_image)
print('odoo_server:', inst.odoo_server_id.name if inst.odoo_server_id else None)
print('pserver:', inst.pserver_id.name if inst.pserver_id else None)
# Images BTP disponibles
btp = self.env['saas.docker.image'].search([('tag_id.name', 'in', ['BTP', 'BTP-OCR'])])
for b in btp:
    print('BTP_IMG:', b.id, b.image_name, '| status=', b.build_status,
          '| servers=', [(s.id, s.name) for s in b.server_ids])
# Vérifier le compose actuel sur le serveur
try:
    ssh = inst.pserver_id._connect_or_raise()
    out = inst.pserver_id._exec_cmd(
        'cat /home/%s/docker-compose.yml | grep -E "image:|container_name" ' % inst.technical_name,
        ssh, without_return=False, raise_on_error=False)
    print('COMPOSE_ACTUEL:')
    print(''.join(out))
    ssh.close()
except Exception as e:
    print('COMPOSE_ERR:', e)
