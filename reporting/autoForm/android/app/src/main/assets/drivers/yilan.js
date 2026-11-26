Regional.register({
    detect() { if (document.querySelector("#chk_agree") && document.querySelector("#btn_submit")?.value === "我要檢舉") return "start"; },
    start() {
        for (const id of ["chk_19","chk_21","chk_22","chk_20","chk_24","chk_15","chk_agree"]) {
            const box = document.getElementById(id);
            if (box && !box.checked) box.click();
        }
        const next = document.querySelector("#btn_submit");
        if (next && !next.disabled && next.value === "我要檢舉") next.click();
    },
    async fill(f) {
        f.splitPlate("license_plate_prefix","license_plate_suffix");
        f.set("violation_date", [f.date.replace(/-/g,"/"),f.time].filter(Boolean).join(" "), "違規時間");
        f.law("violation_fact_select");
        f.district("location_district");
        await f.road("location_street");
        f.set("location_detail", f.address, "違規地點");
        f.set("violation_fact_desc", f.d.violation_description, "違規敘述");
    }
});
