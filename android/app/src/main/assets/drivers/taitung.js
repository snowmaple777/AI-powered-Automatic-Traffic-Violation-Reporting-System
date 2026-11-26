Regional.register({
    form: "license_plate",
    detect(has) {
        if (/維護中|停機維護/.test(document.title)) return "maintenance";
        if (has("consent_checkbox")) return "start";
    },
    start(f) {
        f.check("consent_checkbox");
        if (f.el("next_btn")) f.click("next_btn");
        else location.assign(f.ctx.config.form_url);
    },
    fill(f) {
        f.dateChoice("violation_date");
        f.clock("violation_hour","violation_minute");
        f.set("license_plate", f.d.license_plate, "車牌");
        f.district("location_district");
        f.set("location_detail", f.address, "違規地點");
        f.law("violation_fact_select");
        if (Regional.visible(f.el("violation_remark"))) f.set("violation_remark", f.d.violation_subcategory, "其他違規事項");
        f.set("violation_content", f.d.violation_description, "違規敘述");
    }
});
