# -*- coding: utf-8 -*-
imgs = self.env['saas.docker.image'].search([], order='id')
print('ALL_IMAGES:', [(i.id, i.name, i.image_name, i.build_status, i.is_default if 'is_default' in i._fields else 'NA') for i in imgs])
