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
MODULES="${MODULES:-s_odoo_saas_master website_saas_landing saas_storage_management}"

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDONS_DIR="$(cd "$TOOLS_DIR/../.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
chown "$ODOO_USER" "$TMP_DIR"

run_odoo() {
    runuser -u "$ODOO_USER" -- env HOME="$ODOO_HOME" "$PY" "$ODOO_BIN" -c "$CONF" -d "$DB" "$@"
}

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
run_odoo -u "${MODULES// /,}" --load-language="$LANGS_ODOO" --stop-after-init --no-http

echo ">> 4/4 enabling the languages on the website"
run_odoo shell --no-http <<PY
website = env['website'].search([], limit=1)
langs = env['res.lang'].with_context(active_test=False).search([('code', 'in', '${LANGS_ODOO}'.split(','))])
website.language_ids = [(6, 0, sorted(set(website.language_ids.ids) | set(langs.ids)))]
env.cr.commit()
print('website languages:', website.language_ids.mapped('code'))
PY

systemctl start "$SERVICE"
echo ">> done. Hard-refresh the browser (Ctrl+Shift+R)."
