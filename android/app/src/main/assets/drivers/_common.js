(() => {
    "use strict";
    // Installed in the trusted main frame only. The native bridge also checks the current step ticket.
    window.AutoForm = Object.freeze({
        log: code => AutoFormBridge.log(code),
        stepDone: (name, json) => AutoFormBridge.stepDone(name, json),
        reportUnmatched: json => AutoFormBridge.reportUnmatched(json),
        requestFile: () => AutoFormBridge.requestFile(window.__afTicket),
        match: (ticket, mode, query, options) => AutoFormBridge.match(ticket, mode, query, options)
    });
    // A few shared configs use Playwright's text selector for links and labels.
    function one(selector) {
        if (!selector || typeof selector !== "string") return null;
        if (!selector.includes(":has-text(")) return document.querySelector(selector);
        for (const part of selector.split(/,\s*(?=[a-z])/i)) {
            const match = /^(.*):has-text\((['"])(.*?)\2\)$/.exec(part);
            const clean = s => s.replace(/\s+/g," ").trim();
            const el = match ? Array.from(document.querySelectorAll(match[1])).find(e => clean(e.textContent).includes(clean(match[3])))
                : document.querySelector(part);
            if (el) return el;
        }
        return null;
    }
    const required = selector => {
        const element = one(selector);
        if (!element) throw new Error("missing control");
        return element;
    };
    function waitFor(test, timeout = 12000) {
        const read = typeof test === "string" ? () => one(test) : test;
        return new Promise((resolve, reject) => {
            const started = Date.now();
            function poll() {
                try {
                    const result = read();
                    if (result) return resolve(result);
                } catch (e) { return reject(e); }
                if (Date.now() - started >= timeout) return reject(new Error("timeout"));
                setTimeout(poll, 150);
            }
            poll();
        });
    }
    function events(element, blur) {
        for (const type of blur ? ["input", "change", "blur"] : ["input", "change"]) {
            element.dispatchEvent(new Event(type, {bubbles: true}));
        }
    }
    function fill(selector, value, blur = false) {
        const element = required(selector);
        const text = String(value ?? "");
        if (element.value !== text) {
            element.value = text;
            events(element, blur);
        }
    }
    function check(selector) {
        const element = required(selector);
        if (!element.checked) element.click(); // Native click emits input/change and runs site handlers.
        if (!element.checked) throw new Error("unchecked");
    }
    function readOptions(selector) {
        return Array.from(required(selector).options, o => ({value: o.value, text: o.text.trim()}));
    }
    function highlight(selector) {
        const element = one(selector);
        if (element) {
            element.style.outline = "3px solid red";
            element.style.outlineOffset = "2px";
        }
    }
    function selectNative(ctx, key, query, mode = "fuzzy") {
        const selector = ctx.config.selectors[key];
        const options = one(selector) ? readOptions(selector) : [];
        const index = AutoForm.match(ctx.ticket, mode, JSON.stringify(query), JSON.stringify(options));
        if (!options[index]) {
            highlight(selector);
            AutoForm.reportUnmatched(JSON.stringify({ticket: ctx.ticket, key}));
            return null;
        }
        const changed = required(selector).selectedIndex !== index;
        if (changed) {
            required(selector).selectedIndex = index;
            events(required(selector), false);
        }
        return {changed};
    }
    async function selectCascade(ctx, parent, child, entry) {
        const selector = ctx.config.selectors[child];
        const signature = () => one(selector) ? JSON.stringify(readOptions(selector)) : "";
        const before = signature();
        const picked = entry && selectNative(ctx, parent, entry.category, "exact");
        if (!picked) {
            for (const key of [parent, child]) {
                highlight(ctx.config.selectors[key]);
                AutoForm.reportUnmatched(JSON.stringify({ticket: ctx.ticket, key}));
            }
            return;
        }
        // A full PostBack discards this promise. Kotlin runs this step again in the new document.
        // If the parent was already selected, the child can be read immediately.
        if (picked.changed) {
            try {
                await waitFor(() => !ctx.active() || (signature() !== before && readOptions(selector).length > 1));
            } catch (_) {
                if (ctx.active()) {
                    highlight(selector);
                    AutoForm.reportUnmatched(JSON.stringify({ticket: ctx.ticket, key: child}));
                }
                return;
            }
        }
        if (ctx.active()) selectNative(ctx, child, entry.subcategory, "exact");
    }
    function selectViolation(ctx, key) {
        const norm = s => s.normalize("NFKC").replace(/\s/g, "");
        const data = ctx.input_data;
        const candidates = [data.violation_subcategory, data.violation_category].filter(Boolean);
        for (const q of candidates) {
            const hit = Object.entries(ctx.catalog).find(([name, entry]) =>
                !name.startsWith("_") && [name, ...(entry.aliases || [])].some(n => norm(n) === norm(q)));
            if (hit) return selectNative(ctx, key, hit[1].queries || [hit[0]], "violation");
        }
        return selectNative(ctx, key, candidates, "violation");
    }
    async function readFile(ctx) {
        const path = AutoForm.requestFile();
        if (!path) throw new Error("attachment unavailable");
        const response = await fetch(path, {credentials:"omit", cache:"no-store", redirect:"error"});
        if (!response.ok) throw new Error("attachment unavailable");
        const blob = await response.blob();
        if (!ctx.active()) throw new Error("page changed");
        const options = {type:blob.type};
        const modified = Number(response.headers.get("X-File-Modified"));
        if (modified > 0) options.lastModified = modified;
        return new File([blob],ctx.filename,options);
    }
    window.AutoFormTools = {one, required, fill, check, readOptions, waitFor, highlight, selectNative, selectCascade, selectViolation, readFile};
})();
