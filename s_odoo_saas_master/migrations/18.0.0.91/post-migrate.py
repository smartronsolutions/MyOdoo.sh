"""Make the Standard plan 36 / month with 2 workers + 5 GB included.

Odoo does not run reliably with a single worker (one HTTP/cron worker gets
blocked and the UI times out), so the entry plan now ships with 2 workers
instead of 1. The price stays at 36 / month, billed annually (36 × 12 = 432).

Existing containers are NOT restarted by this migration: only the
subscription metadata (``base_workers``) is updated. A container that still
runs 1 worker is bumped to 2 on its next renewal, where the renewal wizard
already redeploys it with the new worker count.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # 1. Standard plan price: legacy 14900 (XPF-era) / 26 (EUR-era) -> 36.
    standard = env['product.product'].search(
        [('default_code', 'ilike', 'Standard'), ('active', '=', True)], limit=1)
    if standard and standard.list_price in (0.0, 26.0, 14900.0):
        standard.list_price = 36.0
        _logger.info("Standard plan price set to 36 / month (product %s).", standard.id)

    # 2. Standard containers now include 2 workers + 5 GB in the base plan.
    #    ``workers_count`` is left untouched on purpose: legacy 1-worker
    #    containers keep running until their next renewal.
    instances = env['saas.odoo.instance'].search([])
    updated = env['saas.odoo.instance']
    for instance in instances:
        plan_product = instance.base_plan_product_id
        code = ((plan_product.default_code or plan_product.name or '') if plan_product else '').lower()
        # An unset base plan is treated as Standard (the default/entry plan).
        if code and 'growth' in code:
            continue
        vals = {}
        if (instance.base_workers or 0) < 2:
            vals['base_workers'] = 2
        if not instance.base_storage_gb:
            vals['base_storage_gb'] = 5.0
        if vals:
            instance.write(vals)
            updated |= instance

    _logger.info(
        "Standard plan: %s container(s) now include 2 workers + 5 GB.", len(updated))
