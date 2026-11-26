Regional.register({
    form:"description_textarea", extra:{start_link:"a[href='#/New']"}, next:"start_link",
    reporter:false,
    detect(has) { if (has("sPub_id")) return "gateEmail"; },
    prepare(f) { f.reporter({id:"sPub_id",name:"sPub_nm",phone:"sPubtel",address:"sPubadd",email:"email"}); },
    async fill(f) {
        // A previously verified session can open the entire form immediately.
        for (const [key,value] of Object.entries({sPub_id:f.d.reporter_id?.toUpperCase(),sPub_nm:f.d.reporter_name,
            sPubtel:f.d.reporter_phone,sPubadd:f.d.reporter_address,email:f.d.reporter_email})) {
            const el = f.el(key);
            if (el && !el.disabled && !el.readOnly && !el.value) f.set(key,value);
        }
        const [,month,day] = f.date.split("-");
        if (month && day) await f.menu("date_input",f.date,"monthDay");
        if (f.hour) await f.menu("hour_input",String(Number(f.hour)));
        if (f.minute) await f.menu("minute_input",f.minute);
        await f.menu("plate_type_input","一般","contains");
        f.splitPlate("plate_prefix_input","plate_suffix_input");
        await f.menu("district_input",f.d.district,"district");
        await f.menu("road_input",f.roadName,"exact",true);
        f.addressParts({lane:"lane_input",alley:"alley_input",suballey:"alley_sub_input",number:"num_input",subnumber:"num_sub_input"});
        f.set("address_remark_input",f.local,"地點補充");
        await f.menu("law_input",f.candidates,"violation");
        f.set("description_textarea",f.d.violation_description,"違規敘述");
        f.check("consent_checkbox");
    },
    fileSlot:Regional.multiple
});
