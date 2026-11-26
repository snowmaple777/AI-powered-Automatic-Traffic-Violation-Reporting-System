Regional.register({
    form: "carno1",
    reporter: {name:"name",id:"pid",phone:"tel",address:"address",email:"email"},
    detect(has) {
        const b = AutoFormTools.one(AutoFormSelectors.agreement_next_btn);
        if (has("agreement_next_btn") && /下一步|同意|已閱讀/.test(b.value || b.textContent)) return "start";
    },
    start(f) { f.click("agreement_next_btn"); },
    fill(f) {
        f.splitPlate("carno1","carno2");
        f.district("imparea");
        f.set("impaddress", f.address, "違規地點");
        f.dateChoice("impdate");
        f.clock("hh","mm");
        f.law("subject");
        f.check("dataread");
    }
});
