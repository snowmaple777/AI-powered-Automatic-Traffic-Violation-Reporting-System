Regional.register({
    form: "license_plate",
    detect(has,url) { if (/\/rincase[12]\.aspx$/i.test(url.pathname) && has("step1_btn")) return "start"; },
    start(f) { f.click("step1_btn"); },
    async fill(f) {
        const t = f.d.car_type || "";
        const kind = /輕機|輕型機/.test(t) ? "輕型機車" : /機/.test(t) ? "普通重型機車" : /貨車/.test(t) ? "小貨車" : /大客/.test(t) ? "大客車" : "小客車";
        f.pick("car_class", kind, "車種");
        f.set("license_plate", f.d.license_plate, "車牌");
        f.dateChoice("violation_date", true);
        f.clock("violation_hour","violation_minute");
        f.law("violation_fact_select");
        f.district("location_district");
        await f.road("location_street");
        f.set("location_detail", f.address, "違規地點");
        f.set("violation_fact_desc", f.d.violation_description, "違規敘述");
    }
});
