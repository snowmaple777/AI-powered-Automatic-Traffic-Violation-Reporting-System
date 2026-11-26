Regional.register({
    next: "agreement_link",
    fill(f) {
        f.set("subject", "檢舉 " + (f.d.license_plate || "") + " 交通違規", "主旨");
        f.set("license_plate", f.d.license_plate, "車牌");
        f.district("location_town");
        f.set("location_detail", f.address, "違規地點");
        f.dateChoice("violation_date");
        f.set("violation_time", f.time, "違規時間");
        f.radios("violation_items");
        f.set("violation_description", f.d.violation_description, "違規敘述");
        f.check("agreement_checkbox");
    }
});
