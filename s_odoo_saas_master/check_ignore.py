# -*- coding: utf-8 -*-
imgs = self.env['saas.docker.image'].search([])
for im in imgs:
    print('IMG:', im.id, im.image_name, '| ignored=', repr(im.ignored_requirements_files))
