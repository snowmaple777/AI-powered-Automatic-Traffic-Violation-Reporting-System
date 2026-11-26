(() => {
    "use strict";
    const {one, waitFor, highlight} = AutoFormTools;
    const visible = el => !!el?.getClientRects().length;
    const compact = s => String(s ?? "").normalize("NFKC").replace(/\s+/g, "").replace(/臺/g, "台");
    const usable = o => o.text.trim() && !/^(請|選擇|--)/.test(o.text.trim());
    function notify(el) {
        el.dispatchEvent(new Event("input", {bubbles: true}));
        el.dispatchEvent(new Event("change", {bubbles: true}));
    }
    class Form {
        constructor(ctx) {
            this.ctx = ctx;
            this.s = window.AutoFormSelectors;
            this.d = ctx.input_data || {};
            const d = this.d;
            this.address = d.violation_location || "";
            const plain = s => (s || "").replace(/臺/g,"台").trim();
            this.local = plain(this.address);
            for (const prefix of [plain(ctx.city),plain(d.district).replace(plain(ctx.city),"")]) {
                if (prefix && this.local.startsWith(prefix)) this.local = this.local.slice(prefix.length).trim();
            }
            this.roadName = this.local.match(/^[^與和、，/\d]*?(?:大道|路|街)/)?.[0] || "";
            this.date = (d.violation_date || "").trim().replace(/[./]/g, "-")
                .replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3")
                .replace(/^(\d{4})-(\d{1,2})-(\d{1,2})$/, (_,y,m,day) => [y,m.padStart(2,"0"),day.padStart(2,"0")].join("-"));
            this.time = (d.violation_time || "").trim().replace(/^(\d{2})(\d{2})$/, "$1:$2").replace(/^(\d):/, "0$1:");
            [this.hour, this.minute] = this.time.split(":");
            const plate = (d.license_plate || "").toUpperCase().trim();
            this.plate = plate.includes("-") ? plate.split("-") : [plate.slice(0,3), plate.slice(3)];
            this.candidates = [d.violation_subcategory, d.violation_category].filter(Boolean);
            for (const query of this.candidates) {
                const hit = Object.entries(ctx.catalog).find(([name, entry]) =>
                    !name.startsWith("_") && [name, ...(entry.aliases || [])].some(n => compact(n) === compact(query)));
                if (hit) {
                    this.candidates = hit[1].queries || [hit[0]];
                    this.kind = hit[1].kind;
                    break;
                }
            }
        }
        el(key) { return one(this.s[key]); }
        active() { if (!this.ctx.active()) throw new Error("page changed"); }
        mark(key, label, resolved = false) {
            this.active();
            if (!resolved) highlight(this.s[key]);
            else {
                const el = this.el(key);
                if (el?.style.outline === "red solid 3px" || el?.style.outline === "3px solid red") {
                    el.style.removeProperty("outline");
                    el.style.removeProperty("outline-offset");
                }
            }
            AutoForm.reportUnmatched(JSON.stringify({ticket: this.ctx.ticket, key, label, resolved}));
        }
        set(key, value, label = "表單欄位") {
            if (value == null || value === "") return;
            this.active();
            const control = this.el(key);
            const el = control?.control || control;
            if (!el || el.disabled) { this.mark(key, label); return; }
            if (el.tagName === "SELECT") { this.pick(key, value, label); return; }
            const text = String(value);
            if (el.value !== text) { el.value = text; notify(el); el.dispatchEvent(new Event("blur")); }
            this.mark(key, label, !!el.value);
        }
        reporter(keys = {}) {
            for (const [k,label] of Object.entries({name:"姓名",id:"身分證字號",phone:"電話",address:"通訊地址",email:"電子信箱"})) {
                const key = k in keys ? keys[k] : "reporter_" + k;
                if (key) this.set(key, k === "id" ? this.d.reporter_id?.toUpperCase() : this.d["reporter_" + k], label);
            }
        }
        check(key) {
            this.active();
            const el = this.el(key);
            if (!el) { this.mark(key, "同意欄位"); return; }
            const input = el.control || el.querySelector("input") || el;
            if (!input.checked) el.click();
        }
        click(key) {
            this.active();
            const el = this.el(key);
            if (!el || el.disabled) throw new Error("control unavailable");
            el.click();
        }
        choose(key, options, index, label) {
            if (index < 0) { this.mark(key, label); return false; }
            this.active();
            const el = this.el(key);
            if (el.selectedIndex !== index) { el.selectedIndex = index; notify(el); }
            this.mark(key, label, true);
            return true;
        }
        pick(key, query, label, mode = "exact") {
            const el = this.el(key);
            const options = Array.from(el?.options || []);
            const qs = (Array.isArray(query) ? query : [query]).filter(v => v != null && v !== "").map(String);
            if (!qs.length) { this.mark(key, label); return false; }
            let index = -1;
            if (mode === "exact") {
                for (const q of qs) {
                    index = options.findIndex(o => usable(o) && [o.value,o.text].some(v => compact(v) === compact(q)));
                    if (index >= 0) break;
                }
            } else {
                index = this.match(options.map(o => ({value:o.value,text:o.text.trim()})), qs, mode);
            }
            return this.choose(key, options, index, label);
        }
        match(options, queries, mode) {
            return AutoForm.match(this.ctx.ticket, mode, JSON.stringify(queries), JSON.stringify(options));
        }
        district(key, query = this.d.district) { return this.pick(key, query, "行政區", "district"); }
        law(key) { return this.pick(key, this.candidates, "違規事實", "violation"); }
        radios(key) {
            const els = Array.from(document.querySelectorAll(this.s[key]));
            const items = els.map(e => ({value:e.value, text:e.labels?.[0]?.textContent?.trim() ||
                e.nextSibling?.textContent?.trim() || e.parentElement.textContent.trim()}));
            const i = this.match(items, this.candidates, "violation");
            if (i < 0) this.mark(key, "違規事實"); else if (!els[i].checked) els[i].click();
        }
        splitPlate(first,last) {
            this.set(first, this.plate[0], "車牌前碼"); this.set(last, this.plate[1], "車牌後碼");
        }
        clock(hour,minute) {
            for (const [key,v,label] of [[hour,this.hour,"違規時"],[minute,this.minute,"違規分"]]) {
                if (v != null && v !== "") this.pick(key,[v,String(Number(v))],label);
            }
        }
        dateChoice(key, slash = false) {
            const day = slash ? this.date.replace(/-/g,"/") : this.date;
            this.set(key,day,"違規日期");
        }
        async until(read, timeout = 4000) {
            try { await waitFor(() => !this.ctx.active() || read(), timeout); }
            catch (_) { /* Let the caller mark a missing or unmatched field. */ }
            this.active();
            return read();
        }
        async road(key) {
            const find = () => Array.from(this.el(key)?.options || []).filter(o => usable(o) && this.address.includes(o.text.trim()))
                .sort((a,b) => b.text.trim().length - a.text.trim().length)[0];
            const opt = await this.until(find);
            if (opt) this.pick(key, opt.value, "路段"); else this.mark(key, "路段");
        }
        addressParts(keys) {
            const patterns = {section:/([零一二三四五六七八九十\d]+)段/, lin:/(\d+)鄰/, lane:/(\d+)巷/, alley:/(\d+)弄/,
                street:/(\d+)街/, sublane:/(\d+)衖/, suballey:/之(\d+)弄/, number:/(\d+)(?:之\d+)?號/, subnumber:/(?:號之|之)(\d+)(?:號|$)/, floor:/(\d+)(?:樓|F)/i};
            for (const [part,key] of Object.entries(keys)) this.set(key, this.local.match(patterns[part])?.[1], "路段門牌");
        }
        personTown(town, detail) {
            const addr = this.d.reporter_address || "";
            const opts = Array.from(this.el(town)?.options || []);
            const hit = opts.filter(o => usable(o) && compact(addr).includes(compact(o.text))).sort((a,b)=>b.text.length-a.text.length)[0];
            if (hit) this.pick(town,hit.value,"通訊地址鄉鎮市區");
            else if (addr) this.mark(town,"通訊地址鄉鎮市區");
            this.set(detail, hit ? addr.slice(addr.indexOf(hit.text.trim())+hit.text.trim().length) || addr : addr,"通訊地址");
        }
        async menu(key, query, mode = "exact", search = false) {
            const el = this.el(key);
            if (!el || !query) { this.mark(key, "下拉選單"); return; }
            this.click(key);
            if (search) {
                el.focus();
                el.value = query;
                el.dispatchEvent(new Event("input",{bubbles:true}));
            }
            const items = () => Array.from(document.querySelectorAll(".v-menu__content .v-list-item,.v-overlay--active [role=option]")).filter(visible);
            const qs = Array.isArray(query) ? query : [query];
            const find = () => {
                const nodes = items();
                const opts = nodes.map(e=>({text:e.innerText.trim(),value:e.innerText.trim()}));
                let i;
                if (mode === "monthDay") {
                    const [year, month, day] = this.date.split("-").map(Number);
                    i = opts.findIndex(o => {
                        const parts = compact(o.text).match(/(?:(\d+)年)?(\d{1,2})月(\d{1,2})日/);
                        if (!parts || Number(parts[2]) !== month || Number(parts[3]) !== day) return false;
                        if (parts[1]) return [year, year-1911].includes(Number(parts[1]));
                        // A yearless entry describes the most recent occurrence of this date.
                        const today = new Date(Date.now() + 8*3600000);
                        const thisYear = today.getUTCFullYear();
                        const future = month > today.getUTCMonth()+1 || (month === today.getUTCMonth()+1 && day > today.getUTCDate());
                        return year === thisYear - Number(future);
                    });
                } else i = mode === "contains" ? opts.findIndex(o=>compact(o.text).includes(compact(query))) : this.match(opts,qs,mode);
                return nodes[i];
            };
            const hit = await this.until(find);
            if (hit) hit.click();
            else { el.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",keyCode:27,bubbles:true})); el.blur(); this.mark(key,mode==="violation"?"違規事實":"下拉選單"); }
        }
    }

    async function attach(ctx, plan) {
        const form = new Form(ctx);
        let slot;
        if (plan.fileSlot) slot = await plan.fileSlot(form, ctx);
        else {
            const keys = form.s.file_inputs || form.s.file_uploads;
            slot = {el: keys ? one(keys[ctx.uploadIndex]) : form.el("file_input")};
        }
        const el = slot?.el;
        if (!el || el.disabled || el.type !== "file") throw new Error("attachment slot unavailable");
        const file = await AutoFormTools.readFile(ctx);
        form.active();
        const transfer = new DataTransfer();
        // For a multiple input, publish the complete FileList in a single change event.
        if (slot.append) for (const previous of el.files) transfer.items.add(previous);
        transfer.items.add(file);
        el.files = transfer.files;
        if (el.files.length !== transfer.files.length) throw new Error("file assignment failed");
        if (!slot.defer) notify(el);
        return {attached:true};
    }

    function register(plan) {
        const s = Object.assign(window.AutoFormSelectors, plan.extra || {});
        const has = key => visible(one(s[key]));
        if (window.__afSubmitListener) document.removeEventListener("click",window.__afSubmitListener,true);
        window.__afSubmitListener = e => {
            if (!e.isTrusted) return;
            const button = one(s.submit_btn || s.preview_btn || s.btn_submit);
            AutoForm.stepDone(button?.contains(e.target) ? "manualSubmit":"otherClick",JSON.stringify({ticket:window.__afTicket}));
        };
        document.addEventListener("click",window.__afSubmitListener,true);
        window.AutoFormDriver = {
            detectStep(url) {
                const form = one(s[plan.form || "reporter_name"]);
                if (visible(form) && !form.disabled && !form.closest("fieldset:disabled") && (!plan.ready || plan.ready())) return "fill";
                return plan.detect?.(has,new URL(url)) || (plan.next && has(plan.agree || plan.next) ? "start" : "unknown");
            },
            steps: {
                async start(ctx) {
                    const f = new Form(ctx);
                    if (plan.agree) f.check(plan.agree);
                    if (plan.start) await plan.start(f);
                    else f.click(plan.next);
                },
                async prepare(ctx) { if (plan.prepare) await plan.prepare(new Form(ctx)); },
                async review(ctx) { if (plan.review) await plan.review(new Form(ctx)); },
                async fill(ctx) {
                    const f = new Form(ctx);
                    if (plan.reporter !== false) f.reporter(plan.reporter);
                    await plan.fill(f);
                },
                async cascade(ctx) { if (plan.cascade) await plan.cascade(new Form(ctx)); },
                async details(ctx) { if (plan.details) await plan.details(new Form(ctx)); },
                upload(ctx) { return attach(ctx,plan); }
            }
        };
    }
    function multiple(f,ctx) {
        return {el:f.el("file_input"),append:ctx.uploadIndex > 0,defer:ctx.uploadIndex + 1 < ctx.mediaCount};
    }
    window.Regional = {register,visible,multiple};
})();
