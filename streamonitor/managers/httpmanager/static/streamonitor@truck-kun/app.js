/* Progressive controls around the server-rendered HTMX views. */
(() => {
    "use strict";
    let lastUpdated = Date.now();
    let stale = false;
    let confirmRequest = null;
    let toastTimer;
    const byId = id => document.getElementById(id);
    const addDialog = byId("add-dialog");
    const confirmDialog = byId("confirm-dialog");
    const isRefresh = path => /^(\/refresh\/streamers|\/recording\/nav\/)/.test(path || "");
    const isListMutation = path => /^\/(toggle|remove|streamer-info)\//.test(path || "") && !path.endsWith("/recording");

    function updateFreshness() {
        const seconds = Math.floor((Date.now() - lastUpdated) / 1000);
        const age = seconds < 5 ? "just now" : seconds < 60 ? `${seconds}s ago` : `${Math.floor(seconds / 60)}m ago`;
        byId("freshness").textContent = `${stale ? "Stale · last updated" : "Last updated"} ${age}`;
        byId("refresh-warning").hidden = !stale;
    }
    function updateStorage() {
        const data = document.querySelector("[data-free-space]");
        if (data) document.querySelectorAll("[data-storage]").forEach(el => {
            el.textContent = `${data.dataset.freeSpace} available / ${data.dataset.totalSpace}`;
        });
    }
    function resetFilters() {
        byId("filter-username").value = "";
        byId("filter-site").value = "";
        byId("filter-status").value = "all";
        htmx.trigger("#filter-streamers", "submit");
    }
    function closeAdd() {
        addDialog.close();
        byId("add-streamer-form").reset();
        byId("add-error").hidden = true;
    }
    function showToast() {
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => byId("toast-notifications").replaceChildren(), 6000);
    }

    document.addEventListener("click", event => {
        const button = event.target.closest("[data-action]");
        if (!button) return;
        switch (button.dataset.action) {
            case "open-add": addDialog.showModal(); break;
            case "close-add": closeAdd(); break;
            case "reset-filters": resetFilters(); break;
            case "retry": htmx.trigger(byId("streamers") || byId("nav-bar"), "refresh"); break;
            case "theater": {
                const expanded = byId("video-area").classList.toggle("theater-mode");
                button.setAttribute("aria-pressed", String(expanded));
                break;
            }
            case "dismiss-error": button.closest(".error-modal").remove(); break;
            case "cancel-confirm": confirmDialog.close(); confirmRequest = null; break;
            case "accept-confirm": {
                const issue = confirmRequest;
                confirmRequest = null;
                confirmDialog.close();
                if (issue) issue();
                break;
            }
        }
    });
    for (const dialog of [addDialog, confirmDialog].filter(Boolean)) {
        dialog.addEventListener("click", event => {
            if (event.target !== dialog) return;
            const box = dialog.getBoundingClientRect();
            if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) {
                if (dialog === addDialog) closeAdd();
                else { dialog.close(); confirmRequest = null; }
            }
        });
    }
    if (addDialog) addDialog.addEventListener("cancel", event => { event.preventDefault(); closeAdd(); });
    confirmDialog.addEventListener("cancel", () => { confirmRequest = null; });
    // A polling swap must not remove the element whose deletion is awaiting consent.
    document.body.addEventListener("htmx:beforeRequest", event => {
        const path = event.detail.requestConfig.path;
        if (isRefresh(path) && (confirmDialog.open || document.querySelector(".streamer-more details[open]"))) event.preventDefault();
        if (event.detail.elt.id === "add-streamer-form") byId("add-error").hidden = true;
    });
    document.body.addEventListener("htmx:confirm", event => {
        if (!event.detail.question) return;
        event.preventDefault();
        byId("confirm-title").textContent = event.detail.question.startsWith("Remove") ? "Remove streamer?" : "Delete recording?";
        byId("confirm-message").textContent = event.detail.question;
        confirmRequest = () => event.detail.issueRequest(true);
        confirmDialog.showModal();
    });
    document.body.addEventListener("htmx:beforeSwap", event => {
        const detail = event.detail;
        const path = detail.requestConfig?.path || detail.xhr?.responseURL?.replace(location.origin, "");
        // Refresh errors must never replace the last known good data.
        if (detail.xhr.status >= 400 && isRefresh(path)) {
            detail.shouldSwap = false;
            stale = true;
            updateFreshness();
        }
        // Failed submissions stay in the drawer, preserving every field.
        if (detail.xhr.status >= 400 && /^\/add(?:\?|$)/.test(path || "")) {
            detail.shouldSwap = false;
            const html = new DOMParser().parseFromString(detail.xhr.responseText, "text/html");
            byId("add-error").textContent = html.querySelector(".toast")?.textContent.trim() || "Could not add streamer. Please try again.";
            byId("add-error").hidden = false;
        }
    });
    document.body.addEventListener("htmx:afterRequest", event => {
        const {successful, requestConfig, elt} = event.detail;
        const path = requestConfig.path;
        if (isRefresh(path)) {
            stale = !successful;
            if (successful) lastUpdated = Date.now();
            updateFreshness();
        }
        if (elt.id === "add-streamer-form") {
            if (successful) closeAdd();
            else if (byId("add-error").hidden) {
                byId("add-error").textContent = "Could not reach the server. Please try again.";
                byId("add-error").hidden = false;
            }
        }
        if (successful && isListMutation(path)) htmx.trigger("#streamers", "refresh");
        if (successful && path.endsWith("/recording")) htmx.trigger("#nav-bar", "refresh");
    });
    document.body.addEventListener("htmx:afterSettle", () => { updateStorage(); showToast(); });
    // Do not leave small menus open after activating an action or clicking away.
    document.addEventListener("click", event => {
        document.querySelectorAll("details[open]").forEach(details => {
            if (!details.contains(event.target) || event.target.closest(".options-popover button, .options-popover a")) details.open = false;
        });
    });
    updateStorage();
    updateFreshness();
    setInterval(updateFreshness, 1000);
})();
