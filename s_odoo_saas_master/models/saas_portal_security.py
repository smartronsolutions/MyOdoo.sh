import base64
import logging
import os

import werkzeug.urls

from odoo import _, fields, models
from odoo.addons.auth_totp.models.totp import (
    ALGORITHM, DIGITS, TIMESTEP, TOTP_SECRET_SIZE)
from odoo.http import request

from .saas_notification import saas_email_wrapper, saas_mail_from

_logger = logging.getLogger(__name__)


class ResUsersPortalSecurity(models.Model):
    """Portal Security tab: login alerts, password change and 2FA (via Odoo's auth_totp)."""

    _inherit = 'res.users'

    saas_login_alerts = fields.Boolean(
        string='Login alerts', default=True,
        help="Email the account owner every time this account signs in.")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _saas_as_self(self):
        """This user's own environment (no sudo).

        ``_check_credentials`` and auth_totp's ``_totp_try_setting`` both validate the
        *current* user, so calling them on a sudoed recordset would compare against the
        administrator instead of the customer.
        """
        self.ensure_one()
        return self.env(user=self.id)['res.users'].browse(self.id)

    def _saas_client_ip(self):
        """Best effort client IP (nginx forwards the real address)."""
        try:
            forwarded = request.httprequest.headers.get('X-Forwarded-For')
            if forwarded:
                return forwarded.split(',')[0].strip()
            return request.httprequest.remote_addr or ''
        except Exception:
            return ''

    def _saas_client_device(self):
        try:
            return (request.httprequest.user_agent.string or '')[:90]
        except Exception:
            return ''

    def _saas_security_url(self):
        params = self.env['ir.config_parameter'].sudo()
        base = (params.get_param('saas.portal_base_url')
                or params.get_param('web.base.url') or '').rstrip('/')
        return base + '/my/saas/settings#security'

    def _saas_send_security_mail(self, subject, title, intro, rows):
        """Send an account-security email (login alert, password change, 2FA).

        Always uses the sudoed mail model, so it also works when a portal customer
        triggers it (portal users have no rights on ``mail.mail``).
        """
        self.ensure_one()
        partner = self.partner_id.sudo()
        recipient = partner._saas_notify_recipient()
        if not recipient:
            _logger.warning("Security mail %r skipped: no address for %s", subject, self.login)
            return self.env['mail.mail']
        params = self.env['ir.config_parameter'].sudo()
        mail = self.env['mail.mail'].sudo().create({
            'subject': subject,
            'body_html': saas_email_wrapper(
                title=title, intro=intro, rows=rows,
                cta_label=_("Review your security settings"),
                cta_url=self._saas_security_url(),
            ),
            'email_to': recipient,
            'email_from': saas_mail_from(self.env),
            'auto_delete': True,
        })
        try:
            mail.send()
        except Exception as error:
            _logger.warning("Security mail %r could not be sent: %s", subject, error)
        return mail

    # ------------------------------------------------------------------
    # login alerts
    # ------------------------------------------------------------------
    def _update_last_login(self):
        """Odoo calls this exactly once per successful login.

        ``_check_credentials`` runs on many more code paths (including session
        revalidation), so hooking that would have emailed the customer on every request.
        """
        result = super()._update_last_login()
        for user in self:
            if not user.saas_login_alerts or not user.partner_id:
                continue
            try:
                now = fields.Datetime.context_timestamp(
                    user.partner_id, fields.Datetime.now())
                user._saas_send_security_mail(
                    _("New sign-in to your hosting portal"),
                    _("New sign-in detected"),
                    _("Your hosting account was just used to sign in. If this was not you, "
                      "change your password immediately and sign out all devices."),
                    [('Account', user.login or ''),
                     ('Date', now.strftime('%d %b %Y, %H:%M %Z')),
                     ('IP address', user._saas_client_ip() or '-'),
                     ('Device', user._saas_client_device() or '-')],
                )
            except Exception as error:
                _logger.warning("Login alert for %s failed: %s", user.login, error)
        return result

    # ------------------------------------------------------------------
    # password
    # ------------------------------------------------------------------
    def saas_change_login(self, new_email):
        """Change the sign-in email of the signed-in user.

        For the account owner the contact address follows the login; a team member only
        changes their own login (the partner is shared with the owner).
        """
        self.ensure_one()
        new_email = (new_email or '').strip().lower()
        if '@' not in new_email or '.' not in new_email.split('@')[-1]:
            return {'success': False, 'error': _("Enter a valid email address.")}
        if new_email == (self.login or '').lower():
            return {'success': False, 'error': _("This is already your login email.")}
        other = self.env['res.users'].sudo().with_context(active_test=False).search(
            [('login', '=', new_email), ('id', '!=', self.id)], limit=1)
        if other:
            return {'success': False, 'error': _(
                "%s is already used by another user.") % new_email}
        self.sudo().write({'login': new_email})
        if not self.sudo().saas_team_role:
            self.partner_id.sudo().write({'email': new_email})
        try:
            self._saas_send_security_mail(
                _("Your hosting portal login email was changed"),
                _("Login email changed"),
                _("The sign-in address of your hosting account is now %s. If this was "
                  "not you, contact support right away.") % new_email,
                [('Account', new_email), ('Status', _('Updated'))],
            )
        except Exception as error:
            _logger.warning("Login change mail failed: %s", error)
        return {'success': True, 'message': _(
            "Your login email is now %s.") % new_email}

    def saas_change_password(self, current, new_password, confirm=None):
        """Change the signed-in user's password after checking the current one."""
        self.ensure_one()
        current = (current or '').strip()
        new_password = (new_password or '').strip()
        if not current:
            return {'success': False, 'error': _("Enter your current password.")}
        if len(new_password) < 8:
            return {'success': False, 'error': _(
                "The new password must be at least 8 characters long.")}
        if confirm is not None and new_password != (confirm or '').strip():
            return {'success': False, 'error': _("The two new passwords do not match.")}
        if new_password == current:
            return {'success': False, 'error': _(
                "The new password must be different from the current one.")}
        # ``_saas_as_self()`` because Odoo's password API acts on ``self.env.user``; a
        # sudoed recordset would check the administrator's password instead.
        try:
            # Odoo's own public API: validates the current password and stores the new one
            # (it also revokes the trusted 2FA devices, like the standard portal does).
            self._saas_as_self().change_password(current, new_password)
        except Exception as error:
            message = str(error)
            # Odoo raises UserError("Wrong password") for a bad current password.
            if 'Wrong password' in message or 'wrong password' in message.lower():
                message = _("Your current password is incorrect.")
            return {'success': False, 'error': message or _(
                "Your current password is incorrect.")}
        try:
            self._saas_send_security_mail(
                _("Your hosting portal password was changed"),
                _("Password changed"),
                _("The password of your hosting account was just changed. If this was not "
                  "you, reset it right away and contact support."),
                [('Account', self.login or ''),
                 ('Date', fields.Datetime.context_timestamp(
                     self.partner_id, fields.Datetime.now()).strftime('%d %b %Y, %H:%M %Z')),
                 ('IP address', self._saas_client_ip() or '-')],
            )
        except Exception as error:
            _logger.warning("Password change alert failed for %s: %s", self.login, error)
        return {'success': True, 'message': _("Your password has been updated.")}

    # ------------------------------------------------------------------
    # two-factor authentication (Odoo's auth_totp)
    # ------------------------------------------------------------------
    def saas_totp_issuer(self):
        """Name the authenticator app shows: the instance name, tagged as Odoo.

        It used to be the portal host, so the customer saw "testsaassh.edc.nc" or the
        company name instead of the instance they actually host with us.
        """
        self.ensure_one()
        instance = self.env['saas.odoo.instance'].sudo().search(
            [('partner_id', '=', self.partner_id.id)], order='id', limit=1)
        base = (instance.name if instance else (self.company_id.name or '')) or 'myodoo.nc'
        return '%s (Odoo)' % base

    def saas_totp_uri(self, secret, issuer=None):
        """otpauth URI for the authenticator app (same format Odoo's wizard builds)."""
        self.ensure_one()
        # The secret must travel WITHOUT separators: Google Authenticator rejects a key that
        # still contains the readable groups of 4 ("MZYT JT32 ..."), which showed up as
        # "Couldn't generate code for this account".
        secret = ''.join((secret or '').split())
        issuer = issuer or self.saas_totp_issuer()
        return werkzeug.urls.url_unparse((
            'otpauth', 'totp',
            werkzeug.urls.url_quote('%s:%s' % (issuer, self.login or ''), safe=':'),
            werkzeug.urls.url_encode({
                'secret': secret,
                'issuer': issuer,
                'algorithm': ALGORITHM.upper(),
                'digits': DIGITS,
                'period': TIMESTEP,
            }), ''
        ))

    def saas_totp_start(self):
        """Return a fresh pending secret + QR payload for the setup screen.

        The secret is generated here (exactly like Odoo's own wizard does) and only stored
        on the user once the 6-digit code has been verified by ``_totp_try_setting``.
        """
        self.ensure_one()
        secret = base64.b32encode(os.urandom(TOTP_SECRET_SIZE // 8)).decode()
        secret = ' '.join(map(''.join, zip(*[iter(secret)] * 4)))  # groups of 4, readable
        return {
            'success': True,
            'enabled': bool(self.sudo().totp_enabled),
            'secret': secret,
            'uri': self.saas_totp_uri(secret),
            # The web module already exposes a QR renderer, so no QR library is needed here.
            'qr_url': '/report/barcode/QR/%s?width=220&height=220'
                      % werkzeug.urls.url_quote(self.saas_totp_uri(secret), safe=''),
        }

    def saas_totp_apply(self, secret, code):
        """Verify the 6-digit code and switch 2FA on (delegates to auth_totp)."""
        self.ensure_one()
        secret = (secret or '').replace(' ', '').upper()
        code = (code or '').replace(' ', '')
        if not secret or not code:
            return {'success': False, 'error': _("Scan the QR code and enter the 6-digit code.")}
        try:
            code_int = int(code)
        except ValueError:
            return {'success': False, 'error': _("The verification code must be 6 digits.")}
        # Odoo's own method: verifies the code, stores the secret and refreshes the session
        # token so the customer is not logged out by the change. It requires the user's own
        # environment because it checks ``self == self.env.user``.
        if not self._saas_as_self()._totp_try_setting(secret, code_int):
            return {'success': False, 'error': _(
                "That code is not valid. Check your authenticator app and try again.")}
        try:
            self._saas_send_security_mail(
                _("Two-factor authentication is now enabled"),
                _("Two-factor authentication enabled"),
                _("From now on, signing in requires the 6-digit code from your authenticator app."),
                [('Account', self.login or ''), ('Status', _('Enabled'))],
            )
        except Exception as error:
            _logger.warning("2FA enable alert failed for %s: %s", self.login, error)
        return {'success': True, 'message': _(
            "Two-factor authentication is active. Enter the app code on every sign-in now.")}

    def saas_totp_disable(self, password=None):
        """Switch two-factor authentication off.

        The portal confirms with its own Yes/No dialog, so no password is asked: the caller
        is already authenticated as the account owner. When a password *is* supplied (API or
        older clients) it is still verified.
        """
        self.ensure_one()
        if not self.sudo().totp_enabled:
            return {'success': True, 'message': _("Two-factor authentication is already off.")}
        if password:
            try:
                self._saas_as_self()._check_credentials(password.strip(), {'interactive': True})
            except Exception:
                return {'success': False, 'error': _("Your password is incorrect.")}
        self.sudo().write({'totp_secret': False})
        try:
            self._saas_send_security_mail(
                _("Two-factor authentication was disabled"),
                _("Two-factor authentication disabled"),
                _("The authenticator code is no longer required to sign in. If this was not "
                  "you, enable it again and change your password."),
                [('Account', self.login or ''), ('Status', _('Disabled'))],
            )
        except Exception as error:
            _logger.warning("2FA disable alert failed for %s: %s", self.login, error)
        return {'success': True, 'message': _("Two-factor authentication is off now.")}

    @staticmethod
    def saas_signout_everywhere(uid, keep_sid=None):
        """Drop every stored session of ``uid`` from the filesystem session store."""
        from odoo.http import root
        removed = 0
        store = root.session_store
        for sid in list(store.list()):
            if keep_sid and sid == keep_sid:
                continue
            try:
                session = store.get(sid)
            except Exception:
                continue
            if session and session.uid == uid:
                store.delete(sid)
                removed += 1
        return removed
