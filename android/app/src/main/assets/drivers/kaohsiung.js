(() => {
    "use strict";
    const {one, required, fill, check, selectNative, selectCascade, waitFor} = window.AutoFormTools;
    function detectStep(url, doc) {
        const path = new URL(url).pathname.toLowerCase();
        const s = window.AutoFormSelectors;
        if (url.toLowerCase().includes("displaycaptcha")) return "unknown";
        if (path.endsWith("/statement.aspx")) return doc.querySelector(s.agreement_submit) ? "start" : "unknown";
        if (path.endsWith("/mail.aspx")) return doc.querySelector(s.reporter_name) && doc.querySelector(s.submit_btn) ? "fill" : "unknown";
        // Only a pending, explicitly confirmed submission may consume this signal.
        return doc.readyState === "complete" ? "done" : "unknown";
    }
    function dateText(value) {
        const date = value.trim().replace(/[/.]/g, "-");
        const m = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(date) || /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(date);
        return m ? [m[1], m[2].padStart(2, "0"), m[3].padStart(2, "0")].join("-") : date;
    }
    function mapping(ctx) {
        const compact = text => text.replace(/\s+/g, "");
        const entries = Object.entries(ctx.config.violation_map).filter(([key]) => !key.startsWith("_"));
        for (const q of [ctx.input_data.violation_subcategory, ctx.input_data.violation_category].filter(Boolean)) {
            const hit = entries.find(([name, entry]) => [name, ...(entry.aliases || [])].some(n => compact(n) === compact(q)));
            if (hit) return hit[1];
        }
        return null;
    }
    function attachmentCount(ctx) {
        const row = required(ctx.config.selectors.file_upload_input).closest("td");
        const count = /目前已上傳\s*(\d+)\s*個檔案/.exec(row?.textContent || "");
        return count ? Number(count[1]) : -1;
    }
    // The official submit control is an ASP.NET LinkButton. Its PostBack need not emit a submit event.
    if (window.__afSubmitListener) document.removeEventListener("click", window.__afSubmitListener, true);
    window.__afSubmitListener = event => {
        const s = window.AutoFormSelectors;
        if (event.isTrusted && s) {
            const submit = event.target.closest?.(s.submit_btn);
            AutoForm.stepDone(submit ? "manualSubmit" : "otherClick", JSON.stringify({ticket: window.__afTicket}));
        }
    };
    document.addEventListener("click", window.__afSubmitListener, true);
    window.AutoFormDriver = {
        detectStep,
        steps: {
            start(ctx) {
                const s = ctx.config.selectors;
                s.agreement_checkboxes.forEach(check);
                required(s.agreement_submit).click();
            },
            fill(ctx) {
                const s = ctx.config.selectors, d = ctx.input_data;
                const id = (d.reporter_id || "").toUpperCase();
                check(s.reporter_nationality_domestic);
                for (const key of ["reporter_name", "reporter_address", "reporter_email", "reporter_phone"]) {
                    fill(s[key], d[key]);
                }
                fill(s.reporter_id, id);
                if (id[1] === "1") check(s.reporter_gender_male);
                if (id[1] === "2") check(s.reporter_gender_female);
                for (const key of ["violation_district", "violation_location_district"]) {
                    selectNative(ctx, key, d.district || "", "district");
                }
                const date = dateText(d.violation_date || ""), time = (d.violation_time || "").trim();
                const stamp = [date, time].filter(Boolean).join(" ");
                const hm = /^(\d{1,2}):(\d{2})$/.exec(time);
                // Air Datepicker limits the sliders to the current time while today is selected.
                // Select the incident day in the widget first so its own min/max rules are applied.
                const picker = window.jQuery?.(required(s.violation_date)).data("datepicker");
                if (picker?.selectDate && hm && /^\d{4}-\d{2}-\d{2}$/.test(date)) {
                    const [year, month, day] = date.split("-").map(Number);
                    const eventDate = new Date(year, month - 1, day, Number(hm[1]), Number(hm[2]));
                    if ((!picker.opts.minDate || eventDate >= picker.opts.minDate) &&
                        (!picker.opts.maxDate || eventDate <= picker.opts.maxDate)) picker.selectDate(eventDate);
                }
                fill(s.violation_date, stamp, true);
                if (hm) {
                    for (const [key, value] of [["time_hours", hm[1]], ["time_minutes", hm[2]]]) {
                        const slider = one(s[key]);
                        if (slider) fill(s[key], Number(value));
                    }
                    // Some datepicker versions update the text while sliding.
                    fill(s.violation_date, stamp, true);
                }
                const plate = d.license_plate || "";
                const pair = plate.split("-");
                fill(s.violation_plate_part1, pair.length === 2 ? pair[0].trim() : plate.slice(0, 3));
                fill(s.violation_plate_part2, pair.length === 2 ? pair[1].trim() : plate.slice(3));
                const type = d.car_type || "";
                check(/重機/.test(type) ? s.car_type_heavy_moto : /輕機|機車/.test(type) ? s.car_type_light_moto : s.car_type_auto);
            },
            cascade(ctx) {
                return selectCascade(ctx, "violation_category", "violation_subcategory", mapping(ctx));
            },
            details(ctx) {
                const s = ctx.config.selectors, d = ctx.input_data;
                fill(s.violation_location_address, d.violation_location);
                fill(s.violation_description, d.violation_description);
            },
            async upload(ctx) {
                const input = required(ctx.config.selectors.file_upload_input);
                const count = attachmentCount(ctx);
                if (count < 0 || count >= 3 || ctx.uploadIndex >= ctx.mediaCount) throw new Error("attachment count");
                const file = await AutoFormTools.readFile(ctx);
                if (!ctx.active()) return;
                const transfer = new DataTransfer();
                transfer.items.add(file);
                input.files = transfer.files;
                input.dispatchEvent(new Event("input", {bubbles: true}));
                input.dispatchEvent(new Event("change", {bubbles: true}));
                return {count};
            },
            uploadPost(ctx) {
                const s = ctx.config.selectors;
                if (required(s.file_upload_input).files.length !== 1 || attachmentCount(ctx) !== ctx.baseline) {
                    throw new Error("attachment changed");
                }
                required(s.file_upload_submit).click();
            },
            async receipt(ctx) {
                try {
                    await waitFor(() => !ctx.active() || attachmentCount(ctx) > ctx.baseline, 12000);
                } catch (_) { /* Return the observed count. Kotlin decides whether retry is possible. */ }
                return {count: attachmentCount(ctx)};
            }
        }
    };
})();
