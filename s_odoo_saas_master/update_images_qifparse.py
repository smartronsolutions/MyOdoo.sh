# -*- coding: utf-8 -*-
img5 = self.env['saas.docker.image'].browse(5)
img7 = self.env['saas.docker.image'].browse(7)

for img in (img5, img7):
    reqs = img.python_requirements or ''
    lines = [l.strip() for l in reqs.splitlines() if l.strip()]
    if 'qifparse' not in lines:
        lines.append('qifparse')
        img.write({'python_requirements': '\n'.join(lines) + '\n'})
        print('UPDATED_REQ:', img.id, img.image_name, '->', repr(img.python_requirements))
    else:
        print('ALREADY_HAS_QIF:', img.id, img.image_name)

# Build uniquement sur SSD Nodes (id 1) pour l'image Standard (retrait temporaire de Contabo)
img5.write({'server_ids': [(6, 0, [1])]})
print('IMG5_SERVERS:', [(s.id, s.name) for s in img5.server_ids])
print('IMG7_SERVERS:', [(s.id, s.name) for s in img7.server_ids])
print('DF5:\n' + img5.dockerfile_content)
print('DF7:\n' + img7.dockerfile_content)
