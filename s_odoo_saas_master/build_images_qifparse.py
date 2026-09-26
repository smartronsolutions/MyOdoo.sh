# -*- coding: utf-8 -*-
for img_id in (5, 7):
    img = self.env['saas.docker.image'].browse(img_id)
    try:
        img.action_build_image()
        print('BUILD_OK:', img.id, img.image_name, '| status=', img.build_status)
    except Exception as e:
        print('BUILD_ERR:', img.id, img.image_name, '|', e)
    log = img.build_log or ''
    print('LOG_TAIL_%s:\n%s' % (img.id, log[-2500:]))
