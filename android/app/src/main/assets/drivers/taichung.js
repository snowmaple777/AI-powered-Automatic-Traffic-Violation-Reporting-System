Regional.register({
    agree:"agreement_checkbox", next:"start_button",
    async fill(f) {
        const [year,month,day] = f.date.split("-");
        if (year && month && day) f.set("violation_date_label",String(Number(year)-1911)+month+day,"違規日期");
        f.set("violation_time_label",f.time.replace(":",""),"違規時間");
        await f.menu("vehicle_type","一般","contains");
        f.splitPlate("license_plate_1","license_plate_2");
        await f.menu("location_town",f.d.district,"district");
        await f.menu("location_road",f.roadName,"exact",true);
        f.addressParts({lane:"location_lane",alley:"location_alley",number:"location_number",subnumber:"location_sub_number"});
        f.set("location_remark",f.local,"地點補充");
        await f.menu("violation_fact",f.candidates,"violation");
        f.set("violation_description",f.d.violation_description,"違規敘述");
        f.check("plate_confirm_checkbox");
        f.check("policy_confirm_checkbox");
    },
    fileSlot:Regional.multiple
});
