import logging

_logger = logging.getLogger(__name__)

# Portal overrides that used to inject a "Storage Upgrade" card into the instance
# "Instance Controls" section and a "Storage" link in the top navigation.
LEGACY_VIEWS = (
    'saas_storage_management.portal_instance_page_storage_inherit',
    'saas_storage_management.myodoo_topbar_storage_inherit',
)


def migrate(cr, version):
    """Remove the legacy storage-upgrade portal overrides.

    Storage is now a dedicated tab inside the instance workspace, so these overrides
    must not be applied any more. Removing them from the manifest is not enough:
    the existing ``ir.ui.view`` records have to be unlinked explicitly.
    """
    for xmlid in LEGACY_VIEWS:
        module, name = xmlid.split('.', 1)
        cr.execute(
            "SELECT res_id FROM ir_model_data WHERE module = %s AND name = %s",
            (module, name),
        )
        row = cr.fetchone()
        if not row:
            continue
        view_id = row[0]
        cr.execute("DELETE FROM ir_ui_view WHERE id = %s", (view_id,))
        cr.execute(
            "DELETE FROM ir_model_data WHERE module = %s AND name = %s",
            (module, name),
        )
        _logger.info(
            "Removed legacy storage portal override %s (view %s)", xmlid, view_id
        )
