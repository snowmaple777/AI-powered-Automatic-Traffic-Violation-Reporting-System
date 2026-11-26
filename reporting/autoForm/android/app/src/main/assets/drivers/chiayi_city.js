Regional.register({
    form:"violation_plate", reporter:false,
    detect(has,url) {
        if (url.pathname.includes("/draft-selection")) return "gateDraft";
        if (has("login_email") && url.pathname.includes("/user-login")) return "gateEmail";
    },
    prepare(f) {
        if (location.pathname.includes("/user-login")) f.set("login_email",f.d.reporter_email,"電子信箱");
    },
    async fill(f) {
        f.check("agreement_checkbox");
        f.set("reporter_id",f.d.reporter_id?.toUpperCase(),"身分證字號");
        const phone = f.d.reporter_phone || "";
        f.set(/^09/.test(phone) ? "reporter_phone_mobile" : "reporter_phone_land",phone,"聯絡電話");
        const addr = f.d.reporter_address || "";
        const city = addr.match(/[\u4e00-\u9fff]{2}[縣市]/)?.[0];
        if (f.pick("reporter_address_city",city,"通訊地址縣市")) {
            await f.until(()=>Array.from(f.el("reporter_address_district")?.options || []).some(o=>o.value && addr.includes(o.text.trim())));
        }
        f.personTown("reporter_address_district","reporter_address_detail");
        f.law("violation_category");
        f.set("violation_plate",f.d.license_plate,"車牌");
        f.check(/機/.test(f.d.car_type) ? "car_type_scooter" : "car_type_car");
        const [y,m,d] = f.date.split("-");
        if (y && m && d) {
            f.pick("violation_year",String(Number(y)-1911),"違規年");
            f.pick("violation_month",[m,String(Number(m))],"違規月");
            await f.until(()=>Array.from(f.el("violation_day")?.options || []).some(o=>o.value && Number(o.value) === Number(d)));
            f.pick("violation_day",[d,String(Number(d))],"違規日");
        }
        f.set("violation_time",f.time,"違規時間");
        f.pick("violation_address_city","嘉義市","違規縣市");
        await f.until(()=>f.el("violation_address_district")?.options.length > 1);
        f.district("violation_address_district");
        f.set("violation_address_detail",f.local,"違規地點");
        f.set("violation_description",f.d.violation_description,"違規敘述");
    },
    async fileSlot(f,ctx) {
        const selector = f.s[ctx.video ? "file_uploads_video" : "file_uploads_image"];
        const inputs = () => document.querySelectorAll(selector);
        const first = inputs()[0];
        if (first?.multiple) return {el:first,append:ctx.typeIndex > 0,defer:!ctx.lastOfType};
        return {el:await f.until(()=>inputs()[ctx.typeIndex])};
    }
});
