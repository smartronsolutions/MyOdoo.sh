# -*- coding: utf-8 -*-
img = self.env['saas.docker.image'].browse(6)
print('BUILD_START:', img.image_name, img.build_status)
try:
    res = img.action_build_image()
    print('BUILD_RESULT:', res['params']['message'])
    print('BUILD_STATUS:', img.build_status)
    print('AUTO_REQS:')
    print(img.auto_requirements or '(vide)')
except Exception as e:
    print('BUILD_EXCEPTION:', type(e).__name__, str(e)[:300])
print('--- BUILD LOG (tail 40) ---')
log = img.build_log or ''
for line in log.splitlines()[-40:]:
    print(line)
