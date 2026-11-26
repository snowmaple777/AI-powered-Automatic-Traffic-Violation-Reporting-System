Regional.register({
    reporter: {address:null},
    detect(has,url) { if (/\/Home\/Index\/?$/i.test(url.pathname) || document.querySelector("[id^='altcha-checkbox']")) return "gate"; },
    fill(f) {
        f.personTown("reporter_address_town","reporter_address_detail");
        f.pick("car_type", /重機|重型機/.test(f.d.car_type) ? "3" : /機|綠牌/.test(f.d.car_type) ? "4" : "1", "車種");
        f.splitPlate("violation_plate_part1","violation_plate_part2");
        f.dateChoice("violation_date");
        f.set("violation_time", f.time, "違規時間");
        f.law("violation_category");
        f.district("violation_district");
        f.set("violation_location_address", f.local, "違規地點");
        f.set("violation_description", f.d.violation_description, "違規敘述");
        f.check("agreement_checkbox");
    }
});
