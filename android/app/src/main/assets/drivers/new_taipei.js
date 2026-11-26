Regional.register({
    agree:"agreement_checkbox", next:"agreement_submit",
    async fill(f) {
        f.check("foreigner_radio_taiwan");
        f.pick("violation_type", /機/.test(f.d.car_type) ? "機車" : "汽車", "車種");
        await f.until(()=>Array.from(f.el("plate_type")?.options || []).some(o=>o.value === "1"));
        f.pick("plate_type","1","車牌類型");
        f.splitPlate("license_prefix","license_suffix");
        f.dateChoice("case_date");
        f.clock("case_hour","case_minute");
        f.pick("is_city_area","新北市","違規縣市");
        await f.until(()=>f.el("district")?.options.length > 1);
        f.district("district");
        await f.road("road");
        f.set("addr_remark",f.local,"地點補充");
        f.law("violation_law");
        f.set("violation_remark",f.d.violation_description,"違規敘述");
    },
    async fileSlot(f,ctx) {
        const slot = () => document.querySelectorAll(f.s.file_input)[ctx.uploadIndex];
        if (!slot()) { f.click("add_file_btn"); await f.until(slot); }
        return {el:slot()};
    }
});
