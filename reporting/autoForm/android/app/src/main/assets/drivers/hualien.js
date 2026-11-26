Regional.register({
    form: "reporter_id",
    detect(has,url) { if (/^\/order\/?$/.test(url.pathname)) return "start"; },
    start(f) {
        const map = f.ctx.config.town_typeids;
        const district = (f.d.district || "").replace(/^花蓮縣/, "").trim();
        const id = map[district] || Object.entries(map).find(([town]) => town.length > 2 && f.address.includes(town))?.[1];
        if (!id) { f.mark("entrance_district","請在官網選擇違規行政區"); return; }
        const url = new URL("/order/iframviolation_list.php",location.href);
        url.protocol = "https:";
        url.search = new URLSearchParams({dir:"order2", menu:"", typeid:"2936", violationtype_id:id});
        location.assign(url);
    },
    fill(f) {
        f.splitPlate("license_plate_prefix","license_plate_suffix");
        f.dateChoice("violation_date");
        f.clock("violation_hour","violation_minute");
        f.set("location_detail", f.address, "違規地點");
        f.radios("violation_fact_radio");
        f.set("violation_fact_remark", f.d.violation_description, "違規敘述");
    }
});
