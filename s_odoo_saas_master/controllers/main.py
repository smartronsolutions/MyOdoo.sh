import logging
import os
import werkzeug.exceptions

from odoo import http
from odoo.http import content_disposition, request
_logger = logging.getLogger(__name__)


class AutoBackup(http.Controller):

    @http.route('/saas_backup/download/<int:backup_id>', type='http', auth="user", methods=['GET'], csrf=False)
    def saas_backup_download(self, backup_id, **kwargs):
        try:
            backup = request.env['saas.odoo.instance.backup'].sudo().browse(backup_id)
            if not backup or not backup.exists():
                return request.not_found()
            user = request.env.user
            if not user._is_admin() and backup.instance_id.partner_id.id != user.partner_id.id:
                raise werkzeug.exceptions.Forbidden()
            headers = [
                ('Content-Type', 'application/octet-stream; charset=binary'),
                ('Content-Disposition', content_disposition(backup.name)),
            ]
            with open(backup.file_path, mode='rb') as f:
                stream = f.read()
            response = request.make_response(stream, headers=headers)
            return response
        except Exception as e:
            if isinstance(e, werkzeug.exceptions.HTTPException):
                raise
            error = "Download backup error: %s" % (str(e) or repr(e))
            _logger.exception(error)
            return request.not_found()

    @http.route('/saas_container_backup/download/<int:backup_id>', type='http', auth='user', methods=['GET'], csrf=False)
    def saas_container_backup_download(self, backup_id, **kwargs):
        backup = request.env['saas.odoo.instance.container.backup'].sudo().browse(backup_id).exists()
        if not backup or not backup.file_path or not os.path.isfile(backup.file_path):
            return request.not_found()
        user = request.env.user
        if not user._is_admin() and backup.instance_id.partner_id.id != user.partner_id.id:
            raise werkzeug.exceptions.Forbidden()
        headers = [
            ('Content-Type', 'application/zip'),
            ('Content-Disposition', content_disposition(backup.name)),
        ]
        with open(backup.file_path, mode='rb') as backup_file:
            return request.make_response(backup_file.read(), headers=headers)
