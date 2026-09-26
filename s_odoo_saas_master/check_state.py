# -*- coding: utf-8 -*-
for img in self.env['saas.docker.image'].browse([5, 6, 7, 8]):
    print('STATE:', img.id, img.image_name, '| status=', img.build_status,
          '| servers=', [(s.id, s.name) for s in img.server_ids])
    print('  py=', repr(img.python_requirements))
