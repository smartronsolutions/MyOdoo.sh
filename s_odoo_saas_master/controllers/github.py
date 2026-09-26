import logging
import hashlib
import hmac
import json
import secrets
import threading
import urllib.parse
import requests
import werkzeug.exceptions
from odoo import http, fields, _
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _github_json(payload, status=200):
    """JSON response for the webhook endpoint (GitHub only looks at the status code)."""
    return Response(
        json.dumps(payload), status=status,
        content_type='application/json; charset=utf-8',
    )


def _github_signature_valid(raw_body, secret):
    """Verify GitHub's ``X-Hub-Signature-256`` when GitHub sends one.

    GitHub signs the payload only when the webhook has a secret configured, so the
    per-instance token carried by the URL stays the guard when no signature arrives.
    """
    signature = request.httprequest.headers.get('X-Hub-Signature-256') or ''
    if not signature:
        return True
    expected = 'sha256=' + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _is_db_concurrency_error(exc):
    """True for PostgreSQL concurrency errors Odoo retries at the request level.

    They must not be converted into a JSON result, otherwise the retry mechanism
    provided by ``odoo.service.model.retrying`` never gets a chance to replay the
    request (the transaction is left aborted).
    """
    if type(exc).__name__ in ('SerializationFailure', 'DeadlockDetected', 'LockNotAvailable'):
        return True
    return 'could not serialize' in str(exc).lower()


GITHUB_OAUTH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_BASE_URL = "https://api.github.com"


class SaasGithubController(http.Controller):

    def _get_oauth_credentials(self):
        """Fetch GitHub client ID and secret from company or system parameters."""
        company = request.env.company
        client_id = (
            company.github_client_id
            or request.env['ir.config_parameter'].sudo().get_param('saas.github_client_id')
            or ''
        ).strip()
        client_secret = (
            company.github_client_secret
            or request.env['ir.config_parameter'].sudo().get_param('saas.github_client_secret')
            or ''
        ).strip()
        return client_id, client_secret

    def _validate_instance_access(self, instance_id):
        instance = request.env['saas.odoo.instance'].sudo().browse(int(instance_id))
        if not instance.exists():
            raise http.BadRequest("Instance not found.")
        partner = request.env.user.partner_id
        is_admin = request.env.user.has_group('s_odoo_saas_master.group_odoo_saas_manager')
        if not is_admin and instance.partner_id.id != partner.id:
            raise http.Forbidden("Access denied.")
        return instance

    @http.route('/saas/github/login', type='http', auth='user', website=True)
    def github_login(self, instance_id=None, **kwargs):
        """Redirect user to GitHub OAuth authorization screen."""
        client_id, _ = self._get_oauth_credentials()
        redirect_to = kwargs.get('redirect', '')

        if not client_id:
            if instance_id:
                return request.redirect(f"/my/saas/odoo-instance/{instance_id}?github_error=oauth_not_configured&open_github_modal=1")
            return request.redirect("/my/saas/settings?github_error=oauth_not_configured#github")

        state = secrets.token_urlsafe(24)
        request.session['github_oauth_state'] = state
        if instance_id:
            request.session['github_oauth_instance_id'] = str(instance_id)
        if redirect_to:
            request.session['github_oauth_redirect'] = redirect_to

        callback_url = request.httprequest.url_root.rstrip('/') + '/saas/github/callback'
        params = {
            'client_id': client_id,
            'redirect_uri': callback_url,
            'scope': 'repo,read:user',
            'state': state,
        }
        oauth_url = f"{GITHUB_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
        return request.redirect(oauth_url)

    @http.route('/saas/github/callback', type='http', auth='user', website=True)
    def github_callback(self, code=None, state=None, error=None, error_description=None, **kwargs):
        """Handle OAuth callback from GitHub."""
        instance_id = request.session.pop('github_oauth_instance_id', None)
        redirect_to = request.session.pop('github_oauth_redirect', None)
        saved_state = request.session.pop('github_oauth_state', None)

        def make_redirect_url(param_str):
            if instance_id:
                return f"/my/saas/odoo-instance/{instance_id}?{param_str}"
            if redirect_to:
                return f"{redirect_to}?{param_str}"
            return f"/my/saas/settings?{param_str}#github"

        if error:
            _logger.warning("GitHub OAuth error returned: %s - %s", error, error_description)
            return request.redirect(make_redirect_url(f"github_error={urllib.parse.quote(error_description or error)}"))

        if not state or state != saved_state:
            _logger.warning("GitHub OAuth state mismatch. Expected: %s, Got: %s", saved_state, state)
            return request.redirect(make_redirect_url("github_error=state_mismatch"))

        if not code:
            return request.redirect(make_redirect_url("github_error=no_code_provided"))

        client_id, client_secret = self._get_oauth_credentials()
        if not client_id or not client_secret:
            return request.redirect(make_redirect_url("github_error=oauth_credentials_missing"))

        callback_url = request.httprequest.url_root.rstrip('/') + '/saas/github/callback'
        token_payload = {
            'client_id': client_id,
            'client_secret': client_secret,
            'code': code,
            'redirect_uri': callback_url,
        }

        try:
            resp = requests.post(
                GITHUB_OAUTH_TOKEN_URL,
                headers={'Accept': 'application/json'},
                data=token_payload,
                timeout=15,
            )
            data = resp.json()
        except Exception as e:
            _logger.exception("Failed to exchange GitHub authorization code")
            return request.redirect(make_redirect_url(f"github_error={urllib.parse.quote(str(e))}"))

        access_token = data.get('access_token')
        if not access_token:
            err_msg = data.get('error_description') or data.get('error') or "Unable to obtain access token."
            _logger.error("GitHub token exchange failure: %s", data)
            return request.redirect(make_redirect_url(f"github_error={urllib.parse.quote(err_msg)}"))

        user_info = {}
        try:
            user_resp = requests.get(
                f"{GITHUB_API_BASE_URL}/user",
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Accept': 'application/vnd.github.v3+json',
                },
                timeout=10,
            )
            if user_resp.status_code == 200:
                user_info = user_resp.json()
        except Exception as e:
            _logger.warning("Failed to fetch GitHub profile: %s", e)

        partner = request.env.user.partner_id.sudo()
        partner_vals = {
            'github_oauth_token': access_token,
            'github_login': user_info.get('login') or '',
            'github_avatar_url': user_info.get('avatar_url') or '',
        }
        partner.write(partner_vals)

        if instance_id:
            try:
                instance = request.env['saas.odoo.instance'].sudo().browse(int(instance_id))
                if instance.exists():
                    instance.write({'github_token': access_token})
            except Exception as e:
                _logger.warning("Could not associate token with instance %s: %s", instance_id, e)

            return request.redirect(f"/my/saas/odoo-instance/{instance_id}?github_action=select_repo")

        return request.redirect(redirect_to or "/my/saas/settings?github_connected=1#github")

    @http.route('/saas/github/status', type='json', auth='user')
    def github_status(self, instance_id=None, **kwargs):
        partner = request.env.user.partner_id
        client_id, _ = self._get_oauth_credentials()
        has_oauth = bool(client_id)

        token = partner.github_oauth_token
        instance_connected = False
        instance_repo = ''
        instance_branch = 'main'
        last_sync = ''
        last_sync_display = ''

        if instance_id:
            instance = self._validate_instance_access(instance_id)
            if instance.github_token:
                token = instance.github_token
            instance_connected = instance.github_connected
            instance_repo = instance.github_repo_name or instance.github_repo_url or ''
            instance_branch = instance.github_branch or 'main'
            if instance.last_redeploy_date:
                last_sync = instance.last_redeploy_date.strftime('%d %b, %H:%M')
                last_sync_display = 'Synced just now'

        return {
            'success': True,
            'has_oauth': has_oauth,
            'user_connected': bool(token),
            'github_login': partner.github_login or '',
            'github_avatar': partner.github_avatar_url or '',
            'instance_connected': instance_connected,
            'instance_repo': instance_repo,
            'instance_branch': instance_branch,
            'last_sync': last_sync,
            'last_sync_display': last_sync_display,
        }

    @http.route('/saas/github/repos', type='json', auth='user')
    def github_repos(self, instance_id=None, token=None, **kwargs):
        partner = request.env.user.partner_id
        active_token = (token or '').strip()

        if not active_token and instance_id:
            instance = self._validate_instance_access(instance_id)
            active_token = instance.github_token or ''

        if not active_token:
            active_token = partner.github_oauth_token or ''

        if not active_token:
            return {
                'success': False,
                'error': _("No GitHub authorization found. Please connect your GitHub account or provide an access token."),
            }

        try:
            github_login = partner.github_login or ''
            if active_token and not github_login:
                try:
                    u_resp = requests.get(
                        f"{GITHUB_API_BASE_URL}/user",
                        headers={
                            'Authorization': f'Bearer {active_token}',
                            'Accept': 'application/vnd.github.v3+json',
                        },
                        timeout=5,
                    )
                    if u_resp.status_code == 200:
                        u_data = u_resp.json()
                        github_login = u_data.get('login') or ''
                        partner.sudo().write({
                            'github_oauth_token': active_token,
                            'github_login': github_login,
                            'github_avatar_url': u_data.get('avatar_url') or '',
                        })
                except Exception:
                    pass

            resp = requests.get(
                f"{GITHUB_API_BASE_URL}/user/repos",
                headers={
                    'Authorization': f'Bearer {active_token}',
                    'Accept': 'application/vnd.github.v3+json',
                },
                params={
                    'per_page': 100,
                    'sort': 'updated',
                    'affiliation': 'owner,collaborator,organization_member',
                },
                timeout=15,
            )

            if resp.status_code != 200:
                err_body = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else resp.text
                msg = err_body.get('message', err_body) if isinstance(err_body, dict) else err_body
                return {
                    'success': False,
                    'error': f"GitHub API error ({resp.status_code}): {msg}",
                }

            repos = []
            for r in resp.json():
                repos.append({
                    'id': r.get('id'),
                    'name': r.get('name'),
                    'full_name': r.get('full_name'),
                    'clone_url': r.get('clone_url'),
                    'html_url': r.get('html_url'),
                    'default_branch': r.get('default_branch') or 'main',
                    'private': r.get('private', False),
                    'description': r.get('description') or '',
                    'updated_at': r.get('updated_at'),
                })

            return {
                'success': True,
                'repos': repos,
                'github_login': github_login or partner.github_login or '',
            }
        except Exception as e:
            _logger.exception("Error fetching GitHub repositories")
            return {'success': False, 'error': str(e)}

    @http.route('/saas/github/branches', type='json', auth='user')
    def github_branches(self, repo_full_name, instance_id=None, token=None, **kwargs):
        partner = request.env.user.partner_id
        active_token = (token or '').strip()

        if not active_token and instance_id:
            instance = self._validate_instance_access(instance_id)
            active_token = instance.github_token or ''

        if not active_token:
            active_token = partner.github_oauth_token or ''

        clean_repo = (repo_full_name or '').strip().replace('.git', '')
        if clean_repo.startswith('https://github.com/'):
            clean_repo = clean_repo[len('https://github.com/'):]
        clean_repo = clean_repo.strip('/')

        headers = {'Accept': 'application/vnd.github.v3+json'}
        if active_token:
            headers['Authorization'] = f'Bearer {active_token}'

        try:
            resp = requests.get(
                f"{GITHUB_API_BASE_URL}/repos/{clean_repo}/branches",
                headers=headers,
                params={'per_page': 100},
                timeout=12,
            )
            if resp.status_code == 200:
                branches = [b.get('name') for b in resp.json()]
                return {'success': True, 'branches': branches}
            return {'success': False, 'error': f"Failed to fetch branches ({resp.status_code})"}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    @http.route('/saas/github/connect-repo', type='json', auth='user')
    def github_connect_repo(self, instance_id, repo_url, branch='main', token=None, repo_name=None, **kwargs):
        instance = self._validate_instance_access(instance_id)
        partner = request.env.user.partner_id

        active_token = (token or '').strip()
        if not active_token:
            active_token = instance.github_token or partner.github_oauth_token or ''

        try:
            instance.action_connect_github(
                repo_url=repo_url,
                branch=branch or 'main',
                token=active_token or None,
                repo_name=repo_name,
            )

            updated_str = instance.last_redeploy_date.strftime('%d %b, %H:%M') if instance.last_redeploy_date else 'Just now'
            return {
                'success': True,
                'repo_url': instance.github_repo_url,
                'repo_name': instance.github_repo_name or instance.github_repo_url,
                'branch': instance.github_branch or 'main',
                'connected': instance.github_connected,
                'last_redeploy_date': updated_str,
                'updated_display': 'Synced just now',
            }
        except Exception as e:
            if _is_db_concurrency_error(e):
                raise
            _logger.exception("Error connecting GitHub repository to instance %s", instance_id)
            return {'success': False, 'error': str(e)}

    @http.route(['/saas/github/resync', '/saas/instance/redeploy'], type='json', auth='user')
    def github_resync(self, instance_id, **kwargs):
        instance = self._validate_instance_access(instance_id)
        try:
            instance.action_redeploy_latest()
            updated_str = instance.last_redeploy_date.strftime('%d %b, %H:%M') if instance.last_redeploy_date else 'Just now'
            return {
                'success': True,
                'last_redeploy_date': updated_str,
                'updated_display': 'Synced just now',
                'repo_name': instance.github_repo_name or instance.github_repo_url or '',
                'branch': instance.github_branch or 'main',
            }
        except Exception as e:
            if _is_db_concurrency_error(e):
                raise
            _logger.exception("Error re-syncing GitHub repository for instance %s", instance_id)
            return {'success': False, 'error': str(e)}

    @http.route('/saas/instance/github-logs', type='json', auth='user')
    def github_logs_data(self, instance_id, limit=40, **kwargs):
        """Log rows for the GitHub Logs tab (reloaded after a manual Re-sync)."""
        instance = self._validate_instance_access(instance_id)
        logs = instance.github_log_ids[:max(1, min(int(limit or 40), 200))]
        labels = dict(request.env['saas.odoo.instance.github.log']._fields['trigger'].selection)
        return {
            'success': True,
            'latest_id': logs[:1].id if logs else 0,
            'total': len(instance.github_log_ids),
            'logs': [{
                'id': log.id,
                'datetime': log.datetime.strftime('%d %b %Y, %H:%M:%S') if log.datetime else '',
                'trigger': log.trigger,
                'trigger_label': labels.get(log.trigger, log.trigger or ''),
                'status': log.status,
                'branch': log.branch or 'main',
                'addon_name': log.addon_name or '',
                'repo': log.repo or '',
                'ref_before': (log.ref_before or '')[:7] or '—',
                'ref_after': (log.ref_after or '')[:7] or '—',
                'commit_count': log.commit_count,
                'file_count': log.file_count,
                'insertions': log.insertions,
                'deletions': log.deletions,
                'commits': log.commits or '',
                'files': log.files or '',
                'message': log.message or '',
                'output': log.output or '',
            } for log in logs],
        }

    @http.route(['/saas/github/disconnect', '/saas/instance/github-disconnect'], type='json', auth='user')
    def github_disconnect(self, instance_id, **kwargs):
        instance = self._validate_instance_access(instance_id)
        try:
            instance.action_disconnect_github()
            return {
                'success': True,
                'message': _("GitHub repository disconnected.")
            }
        except Exception as e:
            if _is_db_concurrency_error(e):
                raise
            return {'success': False, 'error': str(e)}

    @http.route('/saas/github/disconnect-account', type='json', auth='user')
    def github_disconnect_account(self, **kwargs):
        try:
            partner = request.env.user.partner_id.sudo()
            partner.write({
                'github_oauth_token': False,
                'github_login': False,
                'github_avatar_url': False,
            })
            for key in ['github_oauth_state', 'github_oauth_instance_id', 'github_oauth_redirect']:
                request.session.pop(key, None)
            return {'success': True, 'message': _("GitHub account disconnected successfully.")}
        except Exception as e:
            if _is_db_concurrency_error(e):
                raise
            _logger.exception("Error disconnecting GitHub account")
            return {'success': False, 'error': str(e)}

    @http.route('/saas/github/disconnect-all', type='json', auth='user')
    def github_disconnect_all(self, instance_id=None, **kwargs):
        """Fully remove the GitHub integration in one action.

        * removes every custom addon cloned from GitHub (folder + addons_path + restart),
        * clears the GitHub settings stored on the instance,
        * unlinks the GitHub account from the customer profile.
        """
        partner = request.env.user.partner_id.sudo()
        instance = request.env['saas.odoo.instance']
        if instance_id:
            instance = self._validate_instance_access(instance_id)

        errors = []
        if instance:
            try:
                instance.action_disconnect_github()
            except Exception as e:
                if _is_db_concurrency_error(e):
                    raise
                _logger.exception("Error disconnecting GitHub repository from instance %s", instance_id)
                errors.append(str(e))

        try:
            partner.write({
                'github_oauth_token': False,
                'github_login': False,
                'github_avatar_url': False,
            })
        except Exception as e:
            if _is_db_concurrency_error(e):
                raise
            _logger.exception("Error unlinking GitHub account")
            errors.append(str(e))

        for key in ['github_oauth_state', 'github_oauth_instance_id', 'github_oauth_redirect']:
            request.session.pop(key, None)

        return {
            'success': not errors,
            'error': '; '.join(errors) if errors else None,
            'message': _("GitHub disconnected. All custom addons connected from GitHub were removed."),
        }

    @http.route('/saas/github/save-oauth-config', type='json', auth='user')
    def github_save_oauth_config(self, client_id, client_secret, **kwargs):
        """Save GitHub OAuth App credentials in company and system parameters."""
        if not client_id or not client_secret:
            return {'success': False, 'error': _("Client ID and Client Secret are required.")}
        try:
            company = request.env.company.sudo()
            company.write({
                'github_client_id': client_id.strip(),
                'github_client_secret': client_secret.strip(),
            })
            ICP = request.env['ir.config_parameter'].sudo()
            ICP.set_param('saas.github_client_id', client_id.strip())
            ICP.set_param('saas.github_client_secret', client_secret.strip())
            return {'success': True}
        except Exception as e:
            _logger.exception("Failed to save GitHub OAuth credentials")
            return {'success': False, 'error': str(e)}
