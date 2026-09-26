/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { session } from "@web/session";

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
 * Heuristic: did an RPC fail because the visitor is not authenticated (e.g.
 * the session expired between opening the wizard and submitting it)?
 */
export function isAuthenticationError(error) {
    if (!error) {
        return false;
    }
    const name = error.exceptionName || (error.data && error.data.name) || "";
    if (/SessionExpired|AccessError|SessionException|AuthenticationError/i.test(name)) {
        return true;
    }
    if (error.code === 401 || error.code === 403) {
        return true;
    }
    const message = error.message || (error.data && error.data.message) || "";
    return /session (has )?expired|not authenticated|access denied|log ?in required/i.test(message);
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

function interceptEvent(ev, found, action) {
    ev.preventDefault();
    ev.stopPropagation();
    if (ev.stopImmediatePropagation) {
        ev.stopImmediatePropagation();
    }
    const form = found && found.closest ? found.closest("form") : ev.target;
    openAccountRequiredModal({
        action,
        targetId: found && found.id ? found.id : "",
        form: form && form.nodeName === "FORM" ? form : null,
    });
}

// Capture phase: runs before the page-specific handlers, so a guest never
// reaches the deployment code.
document.addEventListener(
    "click",
    (ev) => {
        if (isAuthenticated()) {
            return;
        }
        const found = findAuthAction(ev.target);
        if (found) {
            interceptEvent(ev, found.el, found.action);
        }
    },
    true
);

document.addEventListener(
    "submit",
    (ev) => {
        if (isAuthenticated()) {
            return;
        }
        if (!isAuthRequiredForm(ev.target)) {
            return;
        }
        interceptEvent(ev, ev.target, "order");
    },
    true
);

const SaaSAuth = {
    isAuthenticated,
    isAuthenticationError,
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
