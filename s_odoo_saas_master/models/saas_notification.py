import logging

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)


# event key -> (partner field, default, label, hint)
SAAS_NOTIFY_EVENTS = (
    ('instance_created', 'saas_notify_instance_created', True,
     'Instance created', 'When a new hosting instance finishes deploying.'),
    ('workers_upgrade', 'saas_notify_workers_upgrade', True,
     'Workers upgrade purchased', 'When extra workers are added to an instance.'),
    ('storage_upgrade', 'saas_notify_storage_upgrade', True,
     'Storage upgrade purchased', 'When extra storage is bought for an instance.'),
    ('storage_warning', 'saas_notify_storage_warning', True,
     'Storage 80% full', 'When storage usage passes 80% of the limit.'),
    ('storage_full', 'saas_notify_storage_full', True,
     'Storage limit exceeded', 'When the instance is stopped because storage is full.'),
    ('backup_created', 'saas_notify_backup_created', False,
     'Backup created', 'When a manual or automatic backup is stored.'),
    ('backup_failed', 'saas_notify_backup_failed', True,
     'Backup failed', 'When a backup could not be completed.'),
    ('expiry_reminder', 'saas_notify_expiry_reminder', True,
     'Renewal reminder', 'Reminders 30, 14 and 3 days before expiry.'),
    ('expired', 'saas_notify_expired', True,
     'Instance expired', 'When a subscription expires and the instance is suspended.'),
    ('revoked', 'saas_notify_revoked', True,
     'Instance revoked', 'When an instance is removed after the grace period.'),
    ('invoice', 'saas_notify_invoice', True,
     'Invoice and payment', 'When an invoice is issued or a payment is received.'),
)

# Field names always follow this convention, which lets the controller validate whatever a
# (possibly cached) page posts without importing the mapping above.
SAAS_NOTIFY_FIELD_PREFIX = 'saas_notify_'


def saas_mail_from(env):
    """From address that the configured outgoing server actually accepts.

    A mail server normally has one authenticated mailbox and a ``from_filter``; mail sent
    with any other sender is refused by the provider (that is why the portal mails sat in
    "exception" even though the SMTP connection test succeeded).
    """
    params = env['ir.config_parameter'].sudo()
    server = env['ir.mail_server'].sudo().search([], limit=1)
    server_from = (server.from_filter or '').strip()
    if ',' in server_from:  # a filter may list several addresses
        server_from = server_from.split(',')[0].strip()
    return (params.get_param('mail.default.from')
            or server_from or env.company.email or '')


def saas_email_wrapper(title, intro, rows, cta_label=None, cta_url=None, footer=None):
    """Responsive HTML mail body (inline styles, table based, works in Gmail/Outlook).

    Kept as a plain function so every notification looks identical and no template record
    has to be maintained per event. Built with concatenation (not ``%`` formatting) so the
    ``%`` signs inside the copy can never be mistaken for a format placeholder.
    """
    row_html = ''
    for label, value in rows:
        row_html += (
            '<tr>'
            '<td style="padding:9px 0;color:#5b6b73;font-size:13px;'
            'border-bottom:1px solid #eef2f4">' + str(label) + '</td>'
            '<td style="padding:9px 0;color:#0d3a44;font-size:13px;font-weight:700;'
            'text-align:right;border-bottom:1px solid #eef2f4">' + str(value) + '</td>'
            '</tr>'
        )
    button = ''
    if cta_label and cta_url:
        button = (
            '<tr><td style="padding:22px 0 6px">'
            '<a href="' + cta_url + '" style="display:inline-block;padding:13px 24px;'
            'background:#0fa8a0;color:#ffffff;text-decoration:none;border-radius:10px;'
            'font-size:14px;font-weight:700">' + cta_label + '</a></td></tr>'
        )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
        '<title>' + title + '</title></head>'
        '<body style="margin:0;padding:0;background:#eef4f5;font-family:-apple-system,'
        'BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#eef4f5"><tr><td align="center" style="padding:26px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;background:#ffffff;border-radius:16px;overflow:hidden;'
        'box-shadow:0 6px 20px rgba(11,46,51,.08)">'
        '<tr><td style="background:linear-gradient(135deg,#0fa8a0,#0b6e68);padding:22px 26px">'
        '<div style="color:#ffffff;font-size:17px;font-weight:800;letter-spacing:.02em">'
        + title + '</div></td></tr>'
        '<tr><td style="padding:26px">'
        '<div style="font-size:18px;font-weight:800;color:#0d3a44;margin-bottom:8px">'
        + title + '</div>'
        '<div style="font-size:14px;line-height:1.6;color:#5b6b73;margin-bottom:18px">'
        + intro + '</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + row_html + button + '</table>'
        '<div style="margin-top:22px;padding-top:16px;border-top:1px solid #eef2f4;'
        'font-size:11px;line-height:1.6;color:#93a5ad">'
        + (footer or 'You are receiving this because email notifications are on for '
                     'your account.') + '</div>'
        '</td></tr></table>'
        '<div style="max-width:600px;margin:14px auto 0;font-size:11px;color:#93a5ad;'
        'text-align:center">This email was sent automatically because notifications are '
        'enabled in your hosting portal.</div>'
        '</td></tr></table></body></html>'
    )


class ResPartnerSaasNotification(models.Model):
    """Per-customer notification preferences, sent through the configured SMTP server."""

    _inherit = 'res.partner'

    saas_notify_email = fields.Char(
        string='Notification Email',
        help="Where hosting notifications are sent. Falls back to the contact's email.")

    saas_notify_instance_created = fields.Boolean(string='Notify: instance created', default=True)
    saas_notify_workers_upgrade = fields.Boolean(string='Notify: workers upgrade', default=True)
    saas_notify_storage_upgrade = fields.Boolean(string='Notify: storage upgrade', default=True)
    saas_notify_storage_warning = fields.Boolean(string='Notify: storage 80% full', default=True)
    saas_notify_storage_full = fields.Boolean(string='Notify: storage limit exceeded', default=True)
    saas_notify_backup_created = fields.Boolean(string='Notify: backup created', default=False)
    saas_notify_backup_failed = fields.Boolean(string='Notify: backup failed', default=True)
    saas_notify_expiry_reminder = fields.Boolean(string='Notify: renewal reminder', default=True)
    saas_notify_expired = fields.Boolean(string='Notify: instance expired', default=True)
    saas_notify_revoked = fields.Boolean(string='Notify: instance revoked', default=True)
    saas_notify_invoice = fields.Boolean(string='Notify: invoice and payment', default=True)

    def _saas_notify_wants(self, event):
        """True when this contact asked to receive ``event``."""
        self.ensure_one()
        field_name = SAAS_NOTIFY_FIELD_PREFIX + (event or '')
        if field_name not in self._fields:
            return False
        return bool(self[field_name])

    def _saas_notify_recipient(self):
        self.ensure_one()
        return (self.saas_notify_email or self.email or '').strip()

    @api.model
    def saas_event_map(self):
        """Event metadata for the portal UI (key, label, hint, current state)."""
        partner = self.env.user.partner_id
        return [{
            'key': key,
            'label': label,
            'hint': hint,
            'enabled': bool(partner[field_name]),
        } for key, field_name, _default, label, hint in SAAS_NOTIFY_EVENTS]

    def _saas_notify(self, event, instance=None, title=None, intro=None, rows=None,
                     cta_label=None, cta_url=None):
        """Send one notification email for ``event`` to this customer.

        Uses ``mail.mail``, so Odoo's outgoing mail server (SMTP) is what actually delivers
        it. Returns the created mail record, or an empty recordset when the customer turned
        the event off or no address is known.
        """
        self.ensure_one()
        Mail = self.env['mail.mail']
        if not self._saas_notify_wants(event):
            _logger.debug("Notification %s skipped for %s (disabled).", event, self.display_name)
            return Mail

        recipient = self._saas_notify_recipient()
        if not recipient:
            _logger.warning("Notification %s skipped for %s: no email address.",
                            event, self.display_name)
            return Mail

        labels = dict((key, label) for key, _f, _d, label, _h in SAAS_NOTIFY_EVENTS)
        label = labels.get(event, event)
        instance = instance.sudo() if instance else None
        params = self.env['ir.config_parameter'].sudo()
        base_url = (params.get_param('saas.portal_base_url')
                    or params.get_param('web.base.url') or '').rstrip('/')
        if instance:
            target_url = '%s/my/saas/odoo-instance/%s' % (base_url, instance.id)
        else:
            target_url = base_url

        mail_rows = list(rows or [])
        if instance:
            mail_rows = [('Instance', instance.name)] + mail_rows
        final_rows = [(lbl, val) for lbl, val in mail_rows if val not in (None, '', False)]

        subject = title or ('%s — %s' % (label, instance.name if instance else 'myodoo.nc'))
        body = saas_email_wrapper(
            title=title or label,
            intro=intro or _("Here is the latest update about your hosting."),
            rows=final_rows,
            cta_label=cta_label or _("Open your hosting portal"),
            cta_url=cta_url or target_url,
        )
        mail = Mail.create({
            'subject': subject,
            'body_html': body,
            'email_to': recipient,
            'email_from': saas_mail_from(self.env),
            'auto_delete': True,
        })
        try:
            # force_send pushes it through the outgoing mail server right away. Without a
            # configured server the mail stays in the queue and is sent once SMTP exists.
            mail.sudo().send()
            _logger.info("Notification %s sent to %s (mail id %s).", event, recipient, mail.id)
        except Exception as error:
            _logger.warning("Notification %s could not be sent to %s: %s", event, recipient, error)
        return mail
