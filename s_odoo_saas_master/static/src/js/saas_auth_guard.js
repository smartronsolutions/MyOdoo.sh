/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { session } from "@web/session";
import { rpc } from "@web/core/network/rpc";

/**
 * Global authentication guard for the SaaS frontend.
 *
 * Every action that creates / deploys / upgrades an instance (free trial,
 * "Order & Deploy", "Deploy", renew, ...) requires an authenticated user. When
 * a public visitor triggers one of those actions we must not:
 *   - fire the deployment RPC (which would answer a technical "session
 *     expired" error), nor
 *   - show a raw warning toast.
 *
 * Instead a centred "Account Required" dialog is displayed with three
 * choices: Sign In, Create Account and Cancel. Before leaving the page the
 * configuration currently being filled in is remembered so that, once the
 * visitor comes back logged in, the exact same plan/configuration is restored.
 *
 * The guard works in capture phase on `document`, so it runs before any
 * page-specific handler and no extra plumbing is required on the buttons.
 */

const PENDING_KEY = "saas_pending_auth_action";
const PENDING_MAX_AGE_MS = 6 * 60 * 60 * 1000; // 6 hours
const OVERLAY_ID = "o_saas_account_required";
const AUTH_STATUS_URL = "/saas/auth-status";

// Selectors of the elements that must be authenticated before doing anything.
const AUTH_ACTIONS = [
    { selector: "#startTrialBtn", action: "trial" },
    { selector: "#submitOrderBtn", action: "order" },
    { selector: ".openerp_enterprise_pricing_trial", action: "trial" },
    { selector: ".openerp_enterprise_pricing_buy_now", action: "order" },
    { selector: "a.o_free_trial_button", action: "trial" },
    { selector: "[data-require-auth]", action: "generic" },
];

/** True when the current user is a real (non public) user. */
export function isAuthenticated() {
    return Boolean(session && session.uid && !session.is_public);
}

/**
 * Store the authoritative answer coming from `/saas/auth-status` inside the
 * in-memory session object, so `isAuthenticated()` becomes truthful again on a
 * page whose `odoo.__session_info__` snapshot is stale.
 *
 * The snapshot baked into the HTML is only valid at render time: a page served
 * to an anonymous visitor stays "anonymous" for the guard even after that
 * visitor signs in (login done in another tab, page restored from the
 * back/forward cache, session restored after a reconnection, ...). That is what
 * made an already signed-in customer keep getting "Account Required".
 */
function syncSession(info) {
    if (!info) {
        return false;
    }
    const authenticated = Boolean(info.uid) && !info.is_public;
    try {
        session.uid = info.uid || false;
        session.is_public = !authenticated;
    } catch (e) {
        // Read-only session object: the returned boolean stays correct.
    }
    return authenticated;
}

let pendingAuthCheck = null;

/**
 * Ask the server whether the visitor is really authenticated right now.
 *
 * The page snapshot is used as a fast path; when it claims "not logged in" the
 * server is asked before anything is blocked, so an authenticated visitor is
 * never sent to the login page by mistake. Concurrent calls share one request.
 */
export function verifyAuthentication({ force = false } = {}) {
    if (!force && isAuthenticated()) {
        return Promise.resolve(true);
    }
    if (!pendingAuthCheck) {
        pendingAuthCheck = rpc(AUTH_STATUS_URL, {})
            .then((info) => syncSession(info))
            .catch(() => false)
            .finally(() => {
                pendingAuthCheck = null;
            });
    }
    return pendingAuthCheck;
}

/**
 * Heuristic: did an RPC fail because the visitor is not authenticated (e.g.
 * the session expired between opening the wizard and submitting it)?
 */
export function isAuthenticationError(error) {
    if (!error) {
        return false;
    }
    const name = error.exceptionName || (error.data && error.data.name) || "";
    if (/SessionExpired|SessionException|AuthenticationError/i.test(name)) {
        return true;
    }
    // An AccessError is a *permission* problem, not an authentication one: the
    // visitor is logged in, they just may not perform that action. Reporting it
    // as an auth error used to show "Account Required" to connected customers.
    if (/AccessError/i.test(name) || error.code === 403) {
        return false;
    }
    if (error.code === 401) {
        return true;
    }
    const message = error.message || (error.data && error.data.message) || "";
    return /session (has )?expired|not authenticated|log ?in required/i.test(message);
}

function currentRelativeUrl() {
    return window.location.pathname + window.location.search + window.location.hash;
}

function normalizePath(path) {
    try {
        return new URL(path, window.location.origin).pathname;
    } catch (e) {
        return path || "";
    }
}

function serializeForm(form) {
    const fields = {};
    if (!form) {
        return fields;
    }
    for (const [name, value] of new FormData(form).entries()) {
        if (name === "csrf_token") {
            continue;
        }
        if (name in fields) {
            if (!Array.isArray(fields[name])) {
                fields[name] = [fields[name]];
            }
            fields[name].push(value);
        } else {
            fields[name] = value;
        }
    }
    return fields;
}

function readStorage() {
    try {
        const raw = window.localStorage.getItem(PENDING_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch (e) {
        return null;
    }
}

/** Remember the configuration to restore once the visitor is logged in. */
export function rememberPending({ action, targetId, form, fields } = {}) {
    const data = {
        path: window.location.pathname,
        url: currentRelativeUrl(),
        action: action || "generic",
        targetId: targetId || "",
        formId: form && form.id ? form.id : "",
        formAction: form ? form.getAttribute("action") || "" : "",
        formIndex: form ? Array.prototype.indexOf.call(document.forms, form) : -1,
        fields: Object.assign({}, serializeForm(form), fields || {}),
        ts: Date.now(),
    };
    try {
        window.localStorage.setItem(PENDING_KEY, JSON.stringify(data));
    } catch (e) {
        // Private browsing / storage disabled: the dialog still works, only
        // the "resume where you left off" convenience is lost.
    }
    return data;
}

/** Return the pending action (without consuming it), or null. */
export function peekPending() {
    const data = readStorage();
    if (!data) {
        return null;
    }
    if (Date.now() - (data.ts || 0) > PENDING_MAX_AGE_MS) {
        clearPending();
        return null;
    }
    return data;
}

export function clearPending() {
    try {
        window.localStorage.removeItem(PENDING_KEY);
    } catch (e) {
        // ignore
    }
}

/**
 * Return the pending action only when it belongs to the current page, and
 * consume it at the same time.
 */
export function consumePending() {
    const data = peekPending();
    if (!data) {
        return null;
    }
    if (normalizePath(data.path || "") !== window.location.pathname) {
        // Keep it: the visitor is still navigating towards the right page.
        return null;
    }
    clearPending();
    return data;
}

function resolveForm(pending) {
    if (!pending) {
        return null;
    }
    if (pending.formId) {
        const byId = document.getElementById(pending.formId);
        if (byId && byId.nodeName === "FORM") {
            return byId;
        }
    }
    const forms = Array.from(document.forms || []);
    if (typeof pending.formIndex === "number" && pending.formIndex >= 0 && forms[pending.formIndex]) {
        const candidate = forms[pending.formIndex];
        if (!pending.formAction || candidate.getAttribute("action") === pending.formAction) {
            return candidate;
        }
    }
    if (pending.formAction) {
        return forms.find((form) => form.getAttribute("action") === pending.formAction) || null;
    }
    return null;
}

function applyFormValues(form, fields) {
    if (!form || !fields) {
        return;
    }
    for (const [name, value] of Object.entries(fields)) {
        let elements;
        try {
            elements = form.querySelectorAll('[name="' + CSS.escape(name) + '"]');
        } catch (e) {
            elements = form.querySelectorAll("[name='" + name + "']");
        }
        for (const el of elements) {
            if (el.type === "checkbox" || el.type === "radio") {
                if (Array.isArray(value)) {
                    el.checked = value.includes(el.value);
                } else {
                    el.checked = String(value) === el.value || value === "on" || value === true;
                }
            } else {
                el.value = Array.isArray(value) ? value[0] : value;
            }
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
        }
    }
}

/**
 * Restore the remembered configuration after a successful login/sign-up.
 *
 * Returns the pending payload when something was restored, otherwise null.
 * Page scripts can listen to the "saas:auth-action-restored" document event
 * to recompute their own UI (prices, sub-domain availability, ...).
 */
export function restorePendingAction() {
    if (!isAuthenticated()) {
        return null;
    }
    const pending = consumePending();
    if (!pending) {
        return null;
    }
    const form = resolveForm(pending);
    if (form && pending.fields) {
        applyFormValues(form, pending.fields);
    }
    document.dispatchEvent(new CustomEvent("saas:auth-action-restored", { detail: pending }));
    // Some page scripts reset fields while reacting to the event (e.g. selecting
    // a plan resets the storage count): re-assert the remembered values.
    if (form && pending.fields) {
        applyFormValues(form, pending.fields);
    }
    if (form) {
        const modal = form.closest(".modal");
        if (modal) {
            modal.classList.add("show");
        }
    }
    return pending;
}

function goToLogin(base) {
    const redirect = encodeURIComponent(currentRelativeUrl());
    window.location.href = base + (base.includes("?") ? "&" : "?") + "redirect=" + redirect;
}

function buildModal() {
    const overlay = document.createElement("div");
    overlay.id = OVERLAY_ID;
    overlay.className = "o_saas_auth_overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "o_saas_auth_title");

    const card = document.createElement("div");
    card.className = "o_saas_auth_card";

    const icon = document.createElement("div");
    icon.className = "o_saas_auth_icon";
    icon.innerHTML = '<i class="fa fa-lock" aria-hidden="true"></i>';

    const title = document.createElement("h2");
    title.id = "o_saas_auth_title";
    title.className = "o_saas_auth_title";
    title.textContent = _t("Account Required");

    const message = document.createElement("p");
    message.className = "o_saas_auth_message";
    message.textContent = _t(
        "You need to create an account or sign in before you can create, deploy, or start a trial instance."
    );

    const actions = document.createElement("div");
    actions.className = "o_saas_auth_actions";

    const signIn = document.createElement("button");
    signIn.type = "button";
    signIn.className = "o_saas_auth_btn o_saas_auth_btn_primary";
    signIn.textContent = _t("Sign In");
    signIn.addEventListener("click", () => goToLogin("/web/login"));

    const signUp = document.createElement("button");
    signUp.type = "button";
    signUp.className = "o_saas_auth_btn o_saas_auth_btn_success";
    signUp.textContent = _t("Create Account");
    signUp.addEventListener("click", () => goToLogin("/web/signup"));

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "o_saas_auth_btn o_saas_auth_btn_ghost";
    cancel.textContent = _t("Cancel");
    cancel.addEventListener("click", () => {
        // Keep the remembered configuration: if the visitor signs in later the
        // plan is restored instead of being lost.
        closeAccountRequiredModal();
    });

    actions.appendChild(signIn);
    actions.appendChild(signUp);
    actions.appendChild(cancel);
    card.appendChild(icon);
    card.appendChild(title);
    card.appendChild(message);
    card.appendChild(actions);
    overlay.appendChild(card);

    overlay.addEventListener("click", (ev) => {
        if (ev.target === overlay) {
            closeAccountRequiredModal();
        }
    });
    document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape" && overlay.classList.contains("show")) {
            closeAccountRequiredModal();
        }
    });

    document.body.appendChild(overlay);
    return overlay;
}

/** Open the "Account Required" dialog and remember the pending action. */
export function openAccountRequiredModal(options = {}) {
    rememberPending(options);
    const overlay = document.getElementById(OVERLAY_ID) || buildModal();
    overlay.classList.add("show");
    const primary = overlay.querySelector(".o_saas_auth_btn_primary");
    if (primary) {
        primary.focus();
    }
    return overlay;
}

export function closeAccountRequiredModal() {
    const overlay = document.getElementById(OVERLAY_ID);
    if (overlay) {
        overlay.classList.remove("show");
    }
}

function findAuthAction(target) {
    if (!target || !target.closest) {
        return null;
    }
    for (const descriptor of AUTH_ACTIONS) {
        const el = target.closest(descriptor.selector);
        if (el) {
            const explicit = el.dataset ? el.dataset.requireAuth : null;
            return { el, action: explicit && explicit !== "true" ? explicit : descriptor.action };
        }
    }
    return null;
}

function isAuthRequiredForm(form) {
    if (!form || form.nodeName !== "FORM") {
        return false;
    }
    if (form.dataset && form.dataset.requireAuth !== undefined) {
        return true;
    }
    const marker = form.querySelector(
        "#startTrialBtn, #submitOrderBtn, .openerp_enterprise_pricing_trial, " +
            ".openerp_enterprise_pricing_buy_now, [data-require-auth]"
    );
    if (marker) {
        return true;
    }
    const action = form.getAttribute("action") || "";
    return action.indexOf("/pricing/checkout") !== -1;
}

/**
 * Re-run an action that was interrupted because the page snapshot claimed the
 * visitor was anonymous while the server says they are logged in.
 */
function replayAction(el, action) {
    if (!el || !el.isConnected) {
        return false;
    }
    if (el.nodeName === "FORM") {
        if (el.requestSubmit) {
            el.requestSubmit();
        } else {
            el.submit();
        }
        return true;
    }
    if (action === "order" && el.form) {
        try {
            if (el.form.requestSubmit) {
                el.form.requestSubmit(el);
            } else {
                el.form.submit();
            }
            return true;
        } catch (e) {
            // Fall through to a plain click when the button cannot submit.
        }
    }
    const ev = new MouseEvent("click", { bubbles: true, cancelable: true, view: window });
    // Marked so the capture-phase guard lets this synthetic click through.
    ev.saasAuthRetry = true;
    el.dispatchEvent(ev);
    return true;
}

/**
 * The snapshot said "anonymous", but the server is authoritative: ask it and,
 * when the visitor turns out to be logged in, simply continue the action they
 * started instead of showing the "Account Required" dialog.
 */
function blockForAuthentication(ev, found, action) {
    ev.preventDefault();
    ev.stopPropagation();
    if (ev.stopImmediatePropagation) {
        ev.stopImmediatePropagation();
    }
    const form = found && found.closest ? found.closest("form") : ev.target;
    const options = {
        action,
        targetId: found && found.id ? found.id : "",
        form: form && form.nodeName === "FORM" ? form : null,
    };
    verifyAuthentication({ force: true }).then((authenticated) => {
        if (!authenticated) {
            openAccountRequiredModal(options);
            return;
        }
        // Restore whatever the visitor was filling in before the stale guard
        // interrupted them, then resume.
        if (options.form) {
            restorePendingAction();
        }
        replayAction(found, action);
    });
}

// Capture phase: runs before the page-specific handlers, so a guest never
// reaches the deployment code.
document.addEventListener(
    "click",
    (ev) => {
        if (ev.saasAuthRetry || isAuthenticated()) {
            return;
        }
        const found = findAuthAction(ev.target);
        if (found) {
            blockForAuthentication(ev, found.el, found.action);
        }
    },
    true
);

document.addEventListener(
    "submit",
    (ev) => {
        if (ev.saasAuthRetry || isAuthenticated()) {
            return;
        }
        if (!isAuthRequiredForm(ev.target)) {
            return;
        }
        blockForAuthentication(ev, ev.target, "order");
    },
    true
);

/** True when the current page contains at least one guarded action. */
function hasGuardedAction() {
    if (typeof document === "undefined" || !document.querySelector) {
        return false;
    }
    return AUTH_ACTIONS.some((descriptor) => {
        try {
            return Boolean(document.querySelector(descriptor.selector));
        } catch (e) {
            return false;
        }
    });
}

/**
 * Re-align the in-memory session with the server. Skipped on pages that do not
 * expose any guarded action so anonymous browsing costs no extra RPC.
 */
function refreshAuthentication({ force = false } = {}) {
    if (!force && !hasGuardedAction()) {
        return;
    }
    verifyAuthentication({ force });
}

if (typeof document !== "undefined") {
    const onReady = () => refreshAuthentication();
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", onReady, { once: true });
    } else {
        onReady();
    }
    // A page restored from the back/forward cache keeps its old session
    // snapshot: it must be re-validated, it is exactly the stale case.
    window.addEventListener("pageshow", (ev) => {
        if (ev.persisted) {
            refreshAuthentication({ force: true });
        }
    });
    // Coming back after signing in on another tab.
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && !isAuthenticated()) {
            refreshAuthentication({ force: true });
        }
    });
}

const SaaSAuth = {
    isAuthenticated,
    isAuthenticationError,
    verifyAuthentication,
    refreshAuthentication,
    openAccountRequiredModal,
    closeAccountRequiredModal,
    rememberPending,
    peekPending,
    consumePending,
    clearPending,
    restorePendingAction,
};

if (typeof window !== "undefined") {
    // Convenience handle for legacy / non-module scripts (e.g. the landing page).
    window.SaaSAuth = SaaSAuth;
}

export default SaaSAuth;
