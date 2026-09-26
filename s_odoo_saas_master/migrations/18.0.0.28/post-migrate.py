# Align every instance with the deployed ``workers`` config value and enforce the
# new per-instance maximum.
#
# Admins can change the deployed worker count straight from the instance's Configs
# in the backend. Historically that only changed odoo.conf, so ``workers_count``
# (which the client dashboard shows) kept its old value. ``_sync_workers_to_instance``
# now keeps both in sync; this migration runs it once for existing databases and
# clamps any instance that is above the new limit.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Push the deployed 'workers' config value back onto workers_count
    #    (this also refreshes limit_memory_soft / limit_memory_hard).
    worker_configs = env['saas.odoo.instance.config'].search([('name', '=', 'workers')])
    for config in worker_configs:
        try:
            config._sync_workers_to_instance()
        except Exception:
            _logger.exception("Could not sync workers from config %s", config.id)

    # 2. Clamp any instance still above the maximum.
    Instance = env['saas.odoo.instance']
    max_workers = Instance.MAX_WORKERS_PER_INSTANCE
    for instance in Instance.search([('workers_count', '>', max_workers)]):
        try:
            instance.workers_count = max_workers
        except Exception:
            _logger.exception("Could not clamp workers for instance %s", instance.name)
