Regional.register({
    reporter: {address:null},
    fill(f) {
        f.personTown("reporter_district","reporter_address");
        const type = f.d.car_type || "";
        f.pick("car_type", /重機|重型機/.test(type) ? "3" : /機|綠牌/.test(type) ? "4" : /拖/.test(type) ? "2" : "1", "車種");
        f.splitPlate("plate_first","plate_last");
        const sep = f.el("violation_date")?.type === "datetime-local" ? "T" : " ";
        f.set("violation_date", [f.date,f.time].filter(Boolean).join(sep), "違規時間");
        f.district("violation_district");
        f.set("violation_address", f.local, "違規地點");
        f.law("violation_law");
        f.set("violation_remark", f.d.violation_description, "違規敘述");
        f.check("consent_checkbox");
    }
});
