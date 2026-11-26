Regional.register({
    agree: "agreement_checkbox", next: "agreement_submit",
    fill(f) {
        f.check("reporter_nationality_domestic");
        f.splitPlate("violation_plate_part1","violation_plate_part2");
        f.district("violation_district");
        f.law("violation_category");
        f.set("violation_datetime", [f.date,f.time].filter(Boolean).join(" "), "違規時間");
        f.set("violation_location_address", f.address, "違規地點");
        f.set("violation_description", f.d.violation_description, "違規敘述");
        f.check("agreement_data_collect");
    }
});
