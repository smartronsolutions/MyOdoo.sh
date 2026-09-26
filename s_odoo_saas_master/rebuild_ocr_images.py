# -*- coding: utf-8 -*-
import time
for img_id in (7, 8):
    img = self.env['saas.docker.image'].browse(img_id)
    print('REBUILD_START:', img.image_name, flush=True)
    try:
        res = img.action_build_image()
        print('REBUILD_RESULT:', img.image_name, '->', res['params']['message'], flush=True)
    except Exception as e:
        print('REBUILD_EXCEPTION:', img.image_name, type(e).__name__, str(e)[:200], flush=True)
    print('REBUILD_STATUS:', img.image_name, img.build_status, flush=True)
