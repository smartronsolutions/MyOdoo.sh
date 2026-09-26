from odoo import api, fields, models


class SaasOdooInstanceGithubLog(models.Model):
    """One entry per GitHub sync: what was fetched, which files changed (with + / -).

    ``output`` keeps the raw git output exactly as it printed on the server, so the portal
    can show the same text a developer sees when running ``git pull`` over SSH.
    """

    _name = 'saas.odoo.instance.github.log'
    _description = "SaaS instance GitHub sync log"
    _order = 'id desc'
    _rec_name = 'display_name'

    instance_id = fields.Many2one(
        'saas.odoo.instance', string='Instance', required=True,
        ondelete='cascade', index=True)
    datetime = fields.Datetime(string='Date', default=fields.Datetime.now, required=True, index=True)
    trigger = fields.Selection(
        [('webhook', 'Push (webhook)'), ('manual', 'Manual re-sync'), ('connect', 'Connect')],
        string='Trigger', default='manual', required=True)
    status = fields.Selection(
        [('success', 'Synced'), ('nochange', 'No change'), ('failed', 'Failed')],
        string='Status', default='success', required=True)
    repo = fields.Char(string='Repository')
    addon_name = fields.Char(string='Folder')
    branch = fields.Char(string='Branch')
    ref_before = fields.Char(string='Before')
    ref_after = fields.Char(string='After')
    commits = fields.Text(string='Commits')
    commit_count = fields.Integer(string='Commits count')
    files = fields.Text(string='Files changed')
    file_count = fields.Integer(string='Files count')
    insertions = fields.Integer(string='Insertions')
    deletions = fields.Integer(string='Deletions')
    output = fields.Text(string='Raw git output')
    message = fields.Char(string='Summary')
    display_name = fields.Char(string='Label', compute='_compute_display_name')

    @api.depends('datetime', 'branch', 'status')
    def _compute_display_name(self):
        for log in self:
            log.display_name = '%s %s' % (
                log.datetime.strftime('%d %b %Y %H:%M') if log.datetime else '',
                log.status or '',
            )
