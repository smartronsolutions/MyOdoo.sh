import base64
import hashlib
import hmac
import logging
import secrets
import time

import werkzeug.urls

from odoo import _, api, fields, models

from .saas_notification import saas_email_wrapper, saas_mail_from

_logger = logging.getLogger(__name__)

# What a "Developer" team member may use in the portal. Everything else in the customer
# portal (billing, restarts, delete, settings, ...) stays with the owner and the admins.
SAAS_DEVELOPER_ALLOWED_PREFIXES = (
    '/saas/instance/shell',
    '/saas/instance/live-logs',
    '/saas/instance/github-logs',
    '/saas/instance/logs',
    '/saas/github',
    # backups
    '/saas/instance/create-backup',
    '/saas/instance/delete-backup',
    '/saas/instance/backup-status',
    '/saas/instance/toggle-autobackup',
    # domains
    '/saas/instance/add-domain-name',
    '/saas/instance/verify-domain',
    '/saas/instance/domain-status',
    '/saas/instance/check-domain-name',
    '/saas/instance/remove-domain-name',
    '/saas/settings',
    '/saas/team',
)

SAAS_TEAM_ROLES = [('admin', 'Administrator'), ('developer', 'Developer')]


class ResUsersSaasTeam(models.Model):
    """Customer portal team members.

    A team member is a portal user attached to the *same partner* as the account owner, so
    every instance of that account is visible to them, but with a role that limits what the
    portal shows and allows.
    """

    _inherit = 'res.users'

    saas_team_role = fields.Selection(
        SAAS_TEAM_ROLES, string='Portal role',
        help="Administrator: full access to the account. "
             "Developer: GitHub, GitHub Sync & Deploy, Shell and Odoo Logs only.")
    saas_team_name = fields.Char(
        string='Member name', copy=False,
        help="Display name used in the Team Members list. The team member shares the "
             "account partner, so its name cannot be used for them.")
    saas_team_instance_ids = fields.Many2many(
        'saas.odoo.instance', 'saas_team_instance_rel', 'user_id', 'instance_id',
        string='Allowed instances',
        help="Instances this team member may open. The owner and Administrator members "
             "always see every instance of the account.")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _saas_all_instance_ids(self):
        """Every instance of this account."""
        self.ensure_one()
        return self.env['saas.odoo.instance'].sudo().search(
            [('partner_id', '=', self.partner_id.id)]).ids

    def _saas_instance_domain(self):
        """Search domain of the instances this user is allowed to see.

        The owner (no team role) sees the whole account; a team member only sees the
        instances that were assigned to them.
        """
        self.ensure_one()
        domain = [('partner_id', '=', self.partner_id.id)]
        if not self.sudo().saas_team_role:
            return domain
        return domain + [('id', 'in', self.sudo().saas_team_instance_ids.ids)]

    def _saas_visible_instance_ids(self):
        """Ids of the instances this user may see (every instance for the owner)."""
        self.ensure_one()
        if not self.sudo().saas_team_role:
            return self._saas_all_instance_ids()
        return self.sudo().saas_team_instance_ids.ids

    def _saas_can_open_instance(self, instance):
        """True when this user may open ``instance``."""
        self.ensure_one()
        if not self.sudo().saas_team_role:
            return True
        return instance in self.sudo().saas_team_instance_ids

    def _saas_display_name(self):
        self.ensure_one()
        return self.sudo().saas_team_name or self.login or self.name

    def _saas_is_team_developer(self):
        self.ensure_one()
        return self.sudo().saas_team_role == 'developer'

    def _saas_team_members(self):
        """Every other user of this account."""
        self.ensure_one()
        return self.env['res.users'].sudo().search(
            [('partner_id', '=', self.partner_id.id), ('id', '!=', self.id)],
            order='saas_team_role, login')

    def _saas_check_team_manager(self):
        """Only the account owner (or an internal administrator) manages the team."""
        self.ensure_one()
        if self.env.su or self.env.user._is_admin() or self.sudo().saas_team_role != 'developer':
            return True
        return False

    def _saas_portal_base_url(self):
        params = self.env['ir.config_parameter'].sudo()
        return (params.get_param('saas.portal_base_url')
                or params.get_param('web.base.url') or '').rstrip('/')

    # ------------------------------------------------------------------
    # create / invite
    # ------------------------------------------------------------------
    def saas_team_add(self, name=None, email=None, role=None, password=None, instance_id=None):
        """Create a team member on this account, optionally mailing an invitation."""
        self.ensure_one()
        email = (email or '').strip().lower()
        name = (name or '').strip() or (email.split('@')[0] if '@' in email else '')
        if not email or '@' not in email:
            return {'success': False, 'error': _("Enter a valid email address.")}
        if role not in [key for key, _label in SAAS_TEAM_ROLES]:
            role = 'developer'
        if password:
            password = password.strip()
            if len(password) < 8:
                return {'success': False, 'error': _(
                    "The password must be at least 8 characters long.")}
        User = self.env['res.users'].sudo()
        existing = User.with_context(active_test=False).search(
            [('login', '=', email)], limit=1)
        if existing:
            if existing.partner_id.id != self.partner_id.id:
                # Belongs to a different customer: keep refusing.
                return {'success': False, 'error': _(
                    "%s is already used by another user.") % email}
            # Same account: adding the address again simply grants the instance (and
            # the role) and reactivates the member if they had been removed. This makes
            # add / remove / add work without ever creating a duplicate user.
            wanted = existing.sudo().saas_team_instance_ids
            if instance_id:
                instance = self.env['saas.odoo.instance'].sudo().browse(
                    int(instance_id)).exists()
                wanted |= instance
            if not wanted:
                wanted = self.env['saas.odoo.instance'].sudo().browse(
                    self._saas_all_instance_ids())
            vals = {
                'active': True,
                'saas_team_role': role,
                'saas_team_name': name or existing.saas_team_name,
                'saas_team_instance_ids': [(6, 0, wanted.ids)],
            }
            if password:
                vals['password'] = password.strip()
            existing.write(vals)
            if password:
                message = _("Member updated. They can sign in right away with the "
                            "password you set.")
            else:
                message = self._saas_send_team_invite(existing, email)
            self._log_team_action(self, _("Team member added: %s") % (name or email))
            return {'success': True, 'message': message, 'member': {
                'id': existing.id, 'name': name or existing.saas_team_name or email,
                'email': email, 'role': role,
                'role_label': dict(SAAS_TEAM_ROLES).get(role, role)}}

        vals = {
            'login': email,
            # Sharing the owner's partner is what makes every instance of the account
            # visible to the team member without touching any of the instance queries.
            # ``name`` and ``email`` are deliberately NOT set here: they are related to
            # that shared partner, so writing them would rename the whole account.
            'partner_id': self.partner_id.id,
            'saas_team_name': name,
            'saas_team_role': role,
            # A fresh member starts with every instance of the account; the owner can
            # narrow that down from the member row.
            'saas_team_instance_ids': [(6, 0, [int(instance_id)] if instance_id
                                       else self._saas_all_instance_ids())],
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
            'notification_type': 'email',
        }
        if password:
            vals['password'] = password
        user = User.create(vals)
        if password:
            message = _("Member added. They can sign in right away with the password you set.")
        else:
            message = self._saas_send_team_invite(user, email)
        self._log_team_action(self, _("Team member added: %s") % name)
        return {'success': True, 'message': message, 'member': {
            'id': user.id, 'name': name, 'email': email,
            'role': role, 'role_label': dict(SAAS_TEAM_ROLES).get(role, role),
        }}

    def _saas_token_secret(self):
        params = self.env['ir.config_parameter'].sudo()
        secret = params.get_param('saas.team.token_secret')
        if not secret:
            secret = secrets.token_urlsafe(32)
            params.set_param('saas.team.token_secret', secret)
        return secret

    def _saas_password_token(self, days=14):
        """Signed, stateless token that lets a member set their own password."""
        self.ensure_one()
        expiry = int(time.time()) + days * 86400
        payload = '%s.%s' % (self.id, expiry)
        signature = hmac.new(self._saas_token_secret().encode(),
                             payload.encode(), hashlib.sha256).hexdigest()[:32]
        return base64.urlsafe_b64encode(
            ('%s.%s' % (payload, signature)).encode()).decode()

    @api.model
    def _saas_user_from_password_token(self, token):
        """Resolve the user behind a password token (empty recordset when invalid)."""
        try:
            raw = base64.urlsafe_b64decode((token or '').encode()).decode()
            uid, expiry, signature = raw.split('.')
            if int(expiry) < time.time():
                return self.browse()
            expected = hmac.new(self._saas_token_secret().encode(),
                                ('%s.%s' % (uid, expiry)).encode(),
                                hashlib.sha256).hexdigest()[:32]
            if not secrets.compare_digest(signature, expected):
                return self.browse()
            user = self.sudo().browse(int(uid)).exists()
            return user if user and user.saas_team_role else self.browse()
        except Exception:
            return self.browse()

    def _saas_team_password_url(self, user):
        """Direct link that opens Odoo's password page with the member's address filled in.

        Built from Odoo's own signup token so the member lands straight on the password form
        (no "enter your email" step). ``signup_email`` must be *their* login: they share the
        account partner, so Odoo would otherwise prefill the owner's address.
        """
        user.ensure_one()
        base = self._saas_portal_base_url()
        # Our own page instead of Odoo's signup token: team members share the account
        # partner, which made /web/signup answer "Invalid signup token".
        return '%s/saas/team/password?token=%s' % (
            base, werkzeug.urls.url_quote(user._saas_password_token(), safe=''))

    def _saas_send_team_invite(self, user, email, reset=False):
        """Mail the invitation (or a reset link) through Odoo's outgoing mail server.

        Written by hand instead of ``action_reset_password`` (which stayed silent for a
        fresh portal user): the mail carries our own layout, the role the member gets and a
        button that opens Odoo's reset page, which looks the user up by login.
        """
        base = self._saas_portal_base_url() or ''
        role = dict(SAAS_TEAM_ROLES).get(user.saas_team_role, 'Developer')
        access = (_("GitHub, GitHub Sync & Deploy, Shell (instance + PostgreSQL) and Odoo Logs only.")
                  if user.saas_team_role == 'developer' else
                  _("Full access: billing, workers, storage, backups, GitHub, Shell and logs."))
        owner = self.name or self.login or ''
        account = self.partner_id.name or ''
        login_url = (base + '/web/login') if base else '/web/login'
        mail = self.env['mail.mail'].sudo().create({
            'subject': (_("%s: reset your hosting portal password") % account) if reset else
                       (_("You are invited to the %s hosting portal") % account),
            'body_html': saas_email_wrapper(
                title=(_("Set a new password") if reset else
                       _("You have been added to a hosting account")),
                intro=_("%(owner)s gave you access to the %(account)s hosting portal."
                        ) % {'owner': owner, 'account': account},
                rows=[('Sign in with', email),
                      ('Your role', role),
                      ('What you can access', access),
                      ('Portal', base or '')],
                cta_label=_("Set your password"),
                cta_url=self._saas_team_password_url(user),
                footer=_("Press the button and choose your password straight away — no need "
                         "to type your address again. Afterwards sign in at %(login)s with "
                         "%(email)s.") % {'login': login_url, 'email': email},
            ),
            'email_to': email,
            'email_from': saas_mail_from(self.env),
            'auto_delete': True,
        })
        note = (_(" (no outgoing mail server configured yet, the mail is queued)")
                if not self.env['ir.mail_server'].sudo().search_count([]) else '')
        try:
            mail.send()
            return _("Invitation sent to %(email)s%(note)s.") % {'email': email, 'note': note}
        except Exception as error:
            _logger.warning("Team invite mail failed for %s: %s", email, error)
            return _("Member added, but the invitation email failed: %s") % error

    def saas_team_remove(self, member_id=None, instance_id=None):
        """Revoke access.

        Called from an instance Access tab (``instance_id``) it only removes that
        instance from the member; without it the whole user is archived.
        """
        self.ensure_one()
        member = self.env['res.users'].sudo().browse(int(member_id or 0)).exists()
        if not member or member.partner_id.id != self.partner_id.id or member.id == self.id:
            return {'success': False, 'error': _("Team member not found on this account.")}
        label = member._saas_display_name()
        if instance_id:
            instance = self.env['saas.odoo.instance'].sudo().browse(int(instance_id)).exists()
            if not instance:
                return {'success': False, 'error': _("Instance not found.")}
            member.write({'saas_team_instance_ids': [(3, instance.id)]})
            self._log_team_action(self, _("Access to %s removed for %s") % (instance.name, label))
            return {'success': True, 'message': _(
                "%(name)s can no longer open %(instance)s.") % {
                    'name': label, 'instance': instance.name}}
        member.write({'active': False})
        self._log_team_action(self, _("Team member removed: %s") % label)
        return {'success': True, 'message': _(
            "%s no longer has access to this account.") % label}

    def saas_team_reinvite(self, member_id=None):
        """Send the invitation (fresh set-password link) again."""
        self.ensure_one()
        member = self.env['res.users'].sudo().browse(int(member_id or 0)).exists()
        if not member or member.partner_id.id != self.partner_id.id or member.id == self.id:
            return {'success': False, 'error': _("Team member not found on this account.")}
        message = self._saas_send_team_invite(member, member.login)
        self._log_team_action(self, _("Invitation re-sent to %s") % member._saas_display_name())
        return {'success': True, 'message': message}

    def saas_team_reset_password(self, member_id=None, password=None):
        """Owner-driven password reset for a team member.

        With a password: it is set right away and shown back to the owner so they can hand
        it over. Without one: the member gets the reset mail.
        """
        self.ensure_one()
        member = self.env['res.users'].sudo().browse(int(member_id or 0)).exists()
        if not member or member.partner_id.id != self.partner_id.id or member.id == self.id:
            return {'success': False, 'error': _("Team member not found on this account.")}
        label = member._saas_display_name()
        if password:
            password = password.strip()
            if len(password) < 8:
                return {'success': False, 'error': _(
                    "The password must be at least 8 characters long.")}
            member.write({'password': password, 'active': True})
            self._log_team_action(self, _("Password reset for %s") % label)
            return {'success': True, 'message': _(
                "New password for %(name)s: %(password)s — share it with them now."
            ) % {'name': label, 'password': password}}
        message = self._saas_send_team_invite(member, member.login, reset=True)
        self._log_team_action(self, _("Reset link sent to %s") % label)
        return {'success': True, 'message': message}

    def saas_team_members_for_instance(self, instance_id=None):
        """Team members that may open the given instance."""
        self.ensure_one()
        instance = self.env['saas.odoo.instance'].sudo().browse(int(instance_id or 0)).exists()
        if not instance or instance.partner_id.id != self.partner_id.id:
            return {'success': False, 'error': _("Instance not found.")}
        members = self.env['res.users'].sudo().search(
            [('partner_id', '=', self.partner_id.id), ('id', '!=', self.id),
             ('saas_team_instance_ids', 'in', instance.id)], order='saas_team_role, login')
        return {'success': True, 'instance': instance.name, 'members': [{
            'id': member.id,
            'name': member.saas_team_name or member.login,
            'email': member.login,
            'role': member.saas_team_role or 'developer',
            'role_label': _('Developer') if member.saas_team_role == 'developer'
                          else _('Administrator'),
            'invited': not bool(member.password),
        } for member in members]}

    def saas_team_instances(self, member_id=None):
        """Instance list with the ones this member may open."""
        self.ensure_one()
        member = self.env['res.users'].sudo().browse(int(member_id or 0)).exists()
        if not member or member.partner_id.id != self.partner_id.id:
            return {'success': False, 'error': _("Team member not found on this account.")}
        allowed = set(member.sudo().saas_team_instance_ids.ids)
        instances = self.env['saas.odoo.instance'].sudo().search(
            [('partner_id', '=', self.partner_id.id)], order='id')
        return {'success': True, 'instances': [
            {'id': instance.id, 'name': instance.name, 'selected': instance.id in allowed}
            for instance in instances]}

    def saas_team_set_instances(self, member_id=None, instance_ids=None):
        self.ensure_one()
        member = self.env['res.users'].sudo().browse(int(member_id or 0)).exists()
        if not member or member.partner_id.id != self.partner_id.id:
            return {'success': False, 'error': _("Team member not found on this account.")}
        wanted = [int(value) for value in (instance_ids or []) if str(value).isdigit()]
        instances = self.env['saas.odoo.instance'].sudo().search(
            [('partner_id', '=', self.partner_id.id), ('id', 'in', wanted)])
        member.write({'saas_team_instance_ids': [(6, 0, instances.ids)]})
        self._log_team_action(self, _("Instance access for %s: %s") % (
            member._saas_display_name(), ', '.join(instances.mapped('name')) or _('none')))
        return {'success': True, 'ids': instances.ids, 'message': _(
            "Instance access updated for %s.") % member._saas_display_name()}

    def _log_team_action(self, actor, action):
        """Keep a trace on the customer's portal history."""
        try:
            instance = self.env['saas.odoo.instance'].sudo().search(
                [('partner_id', '=', self.partner_id.id)], order='id', limit=1)
            if instance:
                instance._log_history(
                    action, category='team', level='info', icon='fa-users',
                    summary=_("Portal team"), description=_("%s by %s") % (action, actor.name),
                    source='system')
        except Exception as error:
            _logger.warning("Could not log team action: %s", error)
