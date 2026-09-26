# -*- coding: utf-8 -*-
inst = self.env['saas.odoo.instance'].browse(20)
btp = self.env['saas.docker.image'].browse(6)

# 1. Associer l'image BTP (qifparse) à l'instance
inst.write({'docker_image_id': btp.id})
inst.invalidate_recordset()
print('NEW_IMAGE:', inst.docker_image_id.image_name if inst.docker_image_id else None)
print('NEW_DOCKER_ODOO_IMAGE:', inst.docker_odoo_image)

# 2. Régénérer le docker-compose avec la nouvelle image (sans up, on vérifie d'abord)
ssh = inst.pserver_id._connect_or_raise()
try:
    inst.pserver_id._create_docker_compose_file(inst, ssh)
    out = inst.pserver_id._exec_cmd(
        'grep -E "image:|container_name" /home/%s/docker-compose.yml' % inst.technical_name,
        ssh, without_return=False, raise_on_error=False)
    print('COMPOSE_APRES:')
    print(''.join(out))
finally:
    ssh.close()
print('DONE')
