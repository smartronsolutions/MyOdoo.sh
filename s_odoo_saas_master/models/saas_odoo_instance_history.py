import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


class OdooInstanceHistory(models.Model):
    """Line-by-line activity history of a SaaS instance.

    Every meaningful operation (lifecycle, workers, storage, backups, GitHub, ...) adds
    one row here so the portal History tab can show exactly what happened, when and by
    whom — instead of relying on the generic mail chatter.
    """
    _name = 'saas.odoo.instance.history'
    _description = 'SaaS Odoo Instance Activity History'
    _order = 'datetime desc, id desc'

    instance_id = fields.Many2one(
        'saas.odoo.instance', string='Instance', required=True,
        ondelete='cascade', index=True,
    )
    datetime = fields.Datetime(
        string='Date', default=fields.Datetime.now, required=True, index=True,
    )
    category = fields.Selection([
        ('lifecycle', 'Lifecycle'),
        ('workers', 'Workers'),
        ('storage', 'Storage'),
        ('backup', 'Backup'),
        ('restore', 'Restore'),
        ('github', 'GitHub'),
        ('deployment', 'Deployment'),
        ('domain', 'Domain'),
        ('billing', 'Billing'),
        ('system', 'System'),
    ], string='Category', default='system', required=True, index=True)
    action = fields.Char(string='Action', required=True)
    summary = fields.Char(string='Summary', help="Short one-line value, e.g. '1 → 3 workers'.")
    description = fields.Text(string='Details')
    level = fields.Selection([
        ('info', 'Info'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('danger', 'Error'),
    ], string='Level', default='info', required=True)
    icon = fields.Char(string='Icon', default='fa-info-circle')
    actor_id = fields.Many2one('res.users', string='By', ondelete='set null')
    actor_name = fields.Char(string='By (name)')
    source = fields.Char(
        string='Source', default='portal',
        help="How the change was triggered: portal, backend, cron, system, ...",
    )

    @api.model
    def _log(self, instance, action, category='system', description=None, summary=None,
             level='info', icon=None, source='portal', actor=None):
        """Create one history line for ``instance``. Never raises."""
        if not instance:
            return self.browse()
        try:
            user = actor if actor is not None else self.env.user
            vals = {
                'instance_id': instance.id,
                'action': action,
                'category': category,
                'description': description,
                'summary': summary,
                'level': level,
                'source': source,
                'actor_id': user.id if user and user.id else False,
                'actor_name': (user.name if user and user.id else _('System')),
            }
            if icon:
                vals['icon'] = icon
            if self.env.context.get('history_datetime'):
                vals['datetime'] = self.env.context['history_datetime']
            return self.sudo().create(vals)
        except Exception:  # history must never break the actual operation
            _logger.exception("Could not write history line for instance %s", instance.id)
            return self.browse()
