Regional.register({
    agree: "agreement_checkbox", next: "agreement_submit",
    async fill(f) {
        f.set("violation_subject", (f.d.violation_description || f.d.violation_subcategory || "").slice(0,200), "主旨");
        f.dateChoice("violation_date", true);
        f.clock("violation_time_hours","violation_time_minutes");
        f.splitPlate("violation_plate_part1","violation_plate_part2");
        f.district("violation_district");
        f.law("violation_category");
        f.el("violation_road_search")?.click();
        const road = await f.until(() => Array.from(document.querySelectorAll(f.s.violation_road_items))
            .filter(el=>el.textContent.trim() && f.local.includes(el.textContent.trim()))
            .sort((a,b)=>b.textContent.trim().length-a.textContent.trim().length)[0]);
        if (road) road.click(); else f.mark("violation_road_search","道路");
        f.set("violation_location_address", f.local, "違規地點");
        f.set("violation_description", f.d.violation_description, "違規敘述");
    }
});
