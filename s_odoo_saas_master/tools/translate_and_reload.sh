#!/usr/bin/env bash
#
# Re-generate French/Spanish translations for the custom modules and load them
# into Odoo. Run this whenever new English strings are added to a custom module.
#
#   1. exports a fresh .pot for each module
#   2. machine-translates the missing strings with the local LibreTranslate
#   3. upgrades the modules and loads the translations into Odoo
#   4. makes sure the languages are enabled on the website
#
# Usage (as root):
#   ./translate_and_reload.sh
#   MODULES="s_odoo_saas_master website_saas_landing" LANGS="fr,es" ./translate_and_reload.sh
#
set -euo pipefail

CONF="${ODOO_CONF:-/etc/odoo18.conf}"
DB="${ODOO_DB:-MyOdooSh}"
ODOO_USER="${ODOO_USER:-odoo}"
ODOO_HOME="${ODOO_HOME:-/opt/odoo18}"
SERVICE="${ODOO_SERVICE:-odoo18}"
ODOO_BIN="${ODOO_BIN:-/opt/odoo18/odoo-server/odoo-bin}"
PY="${ODOO_PY:-/opt/odoo18/odoo-server/env/bin/python3}"
LT_URL="${LT_URL:-http://127.0.0.1:5001}"
LANGS="${LANGS:-fr,es,ar,pt,vi,zh}"                       # .po file codes
LANGS_ODOO="${LANGS_ODOO:-fr_FR,es_ES,ar_001,pt_PT,vi_VN,zh_CN}"  # Odoo res.lang codes
MODULES="${MODULES:-payment_epaync s_odoo_saas_master website_saas_landing saas_storage_management}"

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDONS_DIR="$(cd "$TOOLS_DIR/../.." && pwd)"
TMP_DIR="$(mktemp -d)"

# Always leave the service running, even when a step fails midway (the script stops
# it before loading the translations), and always clean the temporary directory.
cleanup() {
    rm -rf "$TMP_DIR"
    if ! systemctl is-active --quiet "$SERVICE"; then
        echo ">> restoring service $SERVICE"
        systemctl start "$SERVICE" || true
    fi
}
trap cleanup EXIT
chown "$ODOO_USER" "$TMP_DIR"

run_odoo() {
    runuser -u "$ODOO_USER" -- env HOME="$ODOO_HOME" "$PY" "$ODOO_BIN" -c "$CONF" -d "$DB" "$@"
}

echo ">> 0/4 upgrading the modules so brand-new source strings are exported"
# --i18n-export reads the terms currently loaded in the database, not the source
# files. Without this upgrade a string added to a view/code since the last module
# update would be missing from the .pot (and therefore never translated).
systemctl stop "$SERVICE" || true
run_odoo -u "${MODULES// /,}" --stop-after-init --no-http >/dev/null
systemctl start "$SERVICE"
sleep 2

echo ">> 1/4 exporting translation templates"
for module in $MODULES; do
    run_odoo --i18n-export="$TMP_DIR/$module.pot" --modules="$module" --stop-after-init --no-http >/dev/null
    # Keep the .pot next to the .po files: Odoo's PO reader merges
    # <module>/i18n/<module>.pot over <lang>.po, so a stale/pot-less template
    # makes the importer lose term references.
    mkdir -p "$ADDONS_DIR/$module/i18n"
    cp "$TMP_DIR/$module.pot" "$ADDONS_DIR/$module/i18n/$module.pot"
    chown "$ODOO_USER" "$ADDONS_DIR/$module/i18n/$module.pot"
    echo "   $module"
done

echo ">> 2/4 machine-translating missing strings via $LT_URL"
for module in $MODULES; do
    "$PY" "$TOOLS_DIR/auto_translate.py" \
        --pot "$TMP_DIR/$module.pot" \
        --out-dir "$ADDONS_DIR/$module/i18n" \
        --langs "$LANGS" \
        --url "$LT_URL" \
        --cache "$ADDONS_DIR/$module/i18n/.auto_translate_cache.json"
done

echo ">> 3/4 loading translations into Odoo (service $SERVICE stopped)"
systemctl stop "$SERVICE" || true
run_odoo --load-language="$LANGS_ODOO" --stop-after-init --no-http

echo ">> 4/4 enabling the languages on the website"
# Odoo 18: the sub-command must come right after the binary, before the options.
runuser -u "$ODOO_USER" -- env HOME="$ODOO_HOME" "$PY" "$ODOO_BIN" shell -c "$CONF" -d "$DB" --no-http <<PY
website = env['website'].search([], limit=1)
langs = env['res.lang'].with_context(active_test=False).search([('code', 'in', '${LANGS_ODOO}'.split(','))])
website.language_ids = [(6, 0, sorted(set(website.language_ids.ids) | set(langs.ids)))]
env.cr.commit()
print('website languages:', website.language_ids.mapped('code'))
PY

systemctl start "$SERVICE"
echo ">> done. Hard-refresh the browser (Ctrl+Shift+R)."
