Regional.register({
    agree: "agreement_checkbox", next: "agreement_submit",
    fill(f) {
        f.set("violation_plate", f.d.license_plate, "車牌");
        f.dateChoice("violation_date", true);
        f.clock("violation_time_hours","violation_time_minutes");
        f.district("violation_district");
        f.set("violation_location_address", f.address, "違規地點");
        f.law("violation_category");
        f.set("violation_description", f.d.violation_description, "違規敘述");
        f.check("agreement_collect");
    }
});
