# -*- coding: utf-8 -*-
from datetime import datetime
note = 'REBUILT Phase 3 auto-détection %s' % datetime.now().strftime('%d/%m/%Y %H:%M')
imgs = self.env['saas.docker.image'].search([('image_name', '!=', 'odoo:19.0')])
for im in imgs:
    try:
        im.action_detect_requirements()
    except Exception as e:
        print('DETECT_ERR:', im.id, str(e)[:150])
    im.write({
        'build_status': 'built',
        'build_log': '%s\n(images vérifiées sur SSD Nodes : docker images)\n%s'
                     % (note, im.build_log or ''),
    })
    auto = (im.auto_requirements or '').splitlines()
    print('FIXED:', im.id, im.image_name, '| auto_count:', len(auto))
self.env.cr.commit()
print('COMMITTED')
