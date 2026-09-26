# -*- coding: utf-8 -*-
for img in self.env['saas.docker.image'].browse([5, 7]):
    print('IMG_DETAIL:', img.id, img.name, '|', img.image_name, '| base=', img.base_image,
          '| status=', img.build_status, '| tag=', img.tag_id.name if img.tag_id else None,
          '| odoo_ver=', img.odoo_version_id.name if img.odoo_version_id else None)
    print('  sys:', repr(img.system_packages))
    print('  py:', repr(img.python_requirements))
    print('  servers:', [(s.id, s.name) for s in img.server_ids])
print('PSERVERS:', [(s.id, s.name, s.ip_address if 'ip_address' in s._fields else 'NA') for s in self.env['saas.pserver'].search([])])
