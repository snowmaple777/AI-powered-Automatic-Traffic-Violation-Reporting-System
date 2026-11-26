Regional.register({
    form: "violation_date", reporter:false,
    detect(has,url) {
        if (/\/ReportStep1\.aspx$/i.test(url.pathname) && has("step1_agree_checkbox")) return "start";
        if (/\/ReportStep2\.aspx$/i.test(url.pathname) && has("reporter_name")) return "introReporter";
        if (/\/ReportStep4\.aspx$/i.test(url.pathname)) return "review";
    },
    agree: "step1_agree_checkbox", start(f) { f.click("step1_next_btn"); },
    async prepare(f) {
        f.reporter({address:null});
        const addr = f.d.reporter_address || "";
        const city = addr.match(/[\u4e00-\u9fff]{2}[縣市]/)?.[0];
        if (f.pick("reporter_city",city,"通訊地址縣市")) {
            await f.until(() => Array.from(f.el("reporter_town")?.options || []).some(o => o.value && addr.includes(o.text.trim())));
        }
        f.personTown("reporter_town","reporter_address_number");
        if (f.ctx.advance && ["reporter_id","reporter_name","reporter_phone","reporter_email","reporter_city","reporter_town","reporter_address_number"]
            .every(key=>f.el(key)?.value && f.el(key).checkValidity()) && city && addr.includes(f.el("reporter_town").selectedOptions[0].text.trim())) {
            AutoForm.stepDone("advancingReporter",JSON.stringify({ticket:f.ctx.ticket}));
            f.click("step2_next_btn");
        }
    },
    review(f) {
        const button = f.el("step3_next_btn");
        if (/\/ReportStep3\.aspx$/i.test(location.pathname) && /下一步/.test(button?.value || button?.textContent) && !button.disabled) button.click();
    },
    async fill(f) {
        f.dateChoice("violation_date");
        f.set("violation_time", f.time, "違規時間");
        f.splitPlate("license_plate_prefix","license_plate_suffix");
        f.law("violation_fact_select");
        f.set("violation_description", f.d.violation_description, "違規敘述");
        f.pick("violation_city", "新竹縣", "違規縣市");
        const matched = f.district("violation_town");
        const districtId = f.el("violation_town")?.value;
        const select = f.el("violation_road_select");
        let found;
        if (matched && select && districtId && f.roadName) {
            try {
                const search = new URLSearchParams({twnId:districtId,q:f.roadName});
                const res = await fetch("/api/address/roads/search?" + search);
                const data = await res.json();
                f.active();
                found = data.results?.filter(item => item.text && f.local.includes(item.text)).sort((a,b)=>b.text.length-a.text.length)[0];
            } catch (_) { f.active(); }
        }
        if (found) {
            select.add(new Option(found.text,found.id,true,true));
            select.dispatchEvent(new Event("change",{bubbles:true}));
            f.set("violation_road_hidden",found.id,"道路");
        } else f.mark("violation_road_select","道路");
        f.addressParts({lin:"violation_neighborhood",lane:"violation_lane",alley:"violation_alley",number:"violation_number"});
        f.set("violation_detail_location",f.local,"地點補充");
    }
});
