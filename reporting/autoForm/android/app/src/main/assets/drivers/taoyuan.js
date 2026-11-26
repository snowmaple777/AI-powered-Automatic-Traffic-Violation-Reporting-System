Regional.register({
    form:"case_date", reporter:false, agree:"agreement_checkbox", next:"agreement_submit",
    detect(has) { if (has("reporter_id")) return "gateIdentity"; },
    prepare(f) { f.reporter(); },
    async fill(f) {
        f.dateChoice("case_date",true);
        f.set("case_time",f.time,"違規時間");
        f.splitPlate("license_prefix","license_suffix");
        const type = f.d.car_type || "";
        f.pick("car_type",/大型重/.test(type) ? "大型重型機車" : /輕|綠牌/.test(type) ? "輕型機車" : /機/.test(type) ? "重型機車" : "汽車","車種");
        f.pick("city","桃園市","違規縣市");
        f.district("district");
        const input = f.el("road_input");
        if (input && f.roadName) {
            input.focus();
            input.value = f.roadName;
            input.dispatchEvent(new Event("input",{bubbles:true}));
            const hit = await f.until(() => Array.from(document.querySelectorAll(".selectize-dropdown .option, .selectize-dropdown [data-selectable]"))
                .find(el=>Regional.visible(el) && el.textContent.trim() === f.roadName));
            if (hit) hit.click(); else f.mark("road_input","道路");
        } else f.mark("road_input","道路");
        f.addressParts({street:"addr_street",lane:"addr_lane",alley:"addr_alley",sublane:"addr_sublane",number:"addr_number"});
        f.set("addr_remark",f.local,"地點補充");
        const parked = f.kind ? f.kind === "static" : f.candidates.some(q=>q.includes("停車"));
        f.pick("violation_type",parked ? "靜態違規" : "動態違規","違規類型");
        const key = parked ? "fact_static" : "fact_dynamic";
        await f.until(()=>f.el(key)?.options.length > 1);
        f.law(key);
        f.set("case_description",f.d.violation_description,"違規敘述");
        f.check("agreement_checkbox_final");
    }
});
