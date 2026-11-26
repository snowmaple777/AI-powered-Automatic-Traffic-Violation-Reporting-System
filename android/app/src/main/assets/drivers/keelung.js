Regional.register({
    form: "license_plate",
    detect(has,url) { if (/\/index\.aspx$/i.test(url.pathname) && has("agreement_checkbox")) return "start"; },
    agree: "agreement_checkbox", start(f) { f.click("agreement_submit"); },
    async fill(f) {
        const t = f.d.car_type || "";
        f.pick("car_type", /大型重/.test(t) ? "6" : /重機|重型機/.test(t) ? "3" : /機|綠牌/.test(t) ? "4" : /拖/.test(t) ? "2" : "1", "車種");
        f.set("license_plate", f.d.license_plate, "車牌");
        f.dateChoice("violation_date");
        f.clock("violation_hour","violation_minute");
        f.district("district");
        await f.road("road");
        f.addressParts({lin:"addr_neighborhood",lane:"addr_lane",alley:"addr_alley",suballey:"addr_suballey",number:"addr_number",subnumber:"addr_subnumber"});
        f.set("addr_cross1", f.local, "地點補充");
        f.law("violation_fact_select");
        f.set("violation_remark", f.d.violation_description, "違規敘述");
    }
});
