Regional.register({
    form: "first_car_number",
    detect(has) {
        if (has("agree_checkbox")) return "start";
        if (has("email")) return "gateEmail";
    },
    agree: "agree_checkbox", start(f) { f.click("agree_submit"); },
    prepare(f) { f.set("email", f.d.reporter_email, "電子信箱"); },
    reporter: {name:"name",id:"id_number",phone:"phone",email:"email",address:"contact_address"},
    async fill(f) {
        f.dateChoice("violated_date");
        f.clock("violated_hour","violated_min");
        const type = f.d.car_type || "";
        f.pick("car_type", /輕|綠牌/.test(type) ? "輕型機車(綠牌)" : /機/.test(type) ? "重型機車(含白紅黃牌)" : "汽車", "車種");
        f.splitPlate("first_car_number","last_car_number");
        f.district("area_district");
        await f.road("area_code");
        f.addressParts({section:"addr1",lane:"addr2",alley:"addr3",number:"addr4",floor:"addr5"});
        f.set("addr_detail", f.local, "地點補充");
        f.law("illegality_code");
        f.set("illegality_details", f.d.violation_description, "違規敘述");
        f.check("read_statement");
    },
    fileSlot(f,ctx) { return {el:document.getElementById("file_input_" + ctx.uploadIndex)}; }
});
