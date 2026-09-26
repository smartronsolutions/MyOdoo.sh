# -*- coding: utf-8 -*-
inst = self.env['saas.odoo.instance'].browse(20)
print('AVANT:', inst.state, inst.operation_state)
# Redéploiement complet : compose up -d avec la nouvelle image
try:
    inst.pserver_id._docker_compose_up(inst)
    print('COMPOSE_UP: OK')
except Exception as e:
    print('COMPOSE_UP_ERR:', str(e)[:300])
# Marquer comme run
inst.write({'state': 'deploy', 'operation_state': 'run'})
inst.invalidate_recordset()
print('APRES:', inst.state, inst.operation_state)
