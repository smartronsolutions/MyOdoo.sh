# Deploy workers are now driven by the instance's ``workers_count`` field instead
# of the (unused) version-level ``workers`` config value. Regenerate the stored
# odoo.conf rows of existing instances so ``workers`` and the per-worker memory
# limits match, and give every instance at least one worker.
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    instances = env['saas.odoo.instance'].search([])
    for instance in instances:
        try:
            if not instance.workers_count or instance.workers_count < 1:
                instance.workers_count = 1
            if instance.config_ids:
                # Rebuild config rows from the Odoo version defaults so the new
                # workers / limit_memory_* overrides are written to odoo.conf.
                instance._generate_instance_config()
        except Exception:
            _logger.exception(
                "Could not regenerate worker config for instance %s", instance.name
            )
