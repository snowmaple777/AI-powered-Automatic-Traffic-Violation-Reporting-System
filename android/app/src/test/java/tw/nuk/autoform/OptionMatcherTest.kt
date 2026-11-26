package tw.nuk.autoform

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class OptionMatcherTest {
    private val matcher = OptionMatcher(JSONObject(requireNotNull(javaClass.getResourceAsStream("/option_synonyms.json"))
        .bufferedReader().use { it.readText() }))
    private val categories = listOf(Option("", "請選擇"),
        Option("A", "違規停車(第56條)"),
        Option("B", "違規臨時停車(第55條)"),
        Option("C", "不當駕駛行為(闖紅燈)"),
        Option("D", "未戴安全帽(第31條)"),
        Option("E", "超速"))
    private val sub = listOf(Option("0", "--請選擇--"),
        Option("1", "在橋樑、隧道、圓環、障礙物對面、人行道、行人穿越道、快車道臨時停車。(55.1.1)"),
        Option("2", "在交岔路口、公車站、促進大眾運輸之公共汽車專用道、消防車出入口五公尺內臨時停車。(55.1.2)"),
        Option("3", "在設有禁止臨時停車標誌、標線處所臨時停車。(55.1.3)"),
        Option("4", "不依順行之方向，或不緊靠道路右側，或單行道不靠右邊臨時停車。(55.1.4)"),
        Option("5", "併排臨時停車。(55.1.5)"))
    private val parking = listOf(Option("551", "在設有禁止臨時停車標誌、標線處所臨時停車"),
        Option("5611", "在禁止臨時停車處所停車"),
        Option("5612", "在設有禁止停車標誌、標線之處所停車"),
        Option("5613", "在人行道停車(非騎樓)"),
        Option("5614", "併排停車"),
        Option("5615", "在身心障礙專用停車位停車"))
    private fun texts(vararg values: String) = values.map { Option(it) }
    private fun best(q: String, opts: List<Option>) = matcher.bestMatch(listOf(q), opts)?.option?.value

    @Test fun normalize() {
        assertEquals(matcher.normalize("臺北市　信義區"), matcher.normalize("台北市信義區"))
        assertEquals(matcher.normalize("ＡＢＣ－１２３４"), matcher.normalize("abc1234"))
        assertTrue("身心障礙" in matcher.normalize("身障車位"))
        assertTrue("併排" in matcher.normalize("雙排停車"))
    }
    @Test fun extractLawRefs() {
        assertTrue(listOf("55","1","1") in matcher.lawRefs("在人行道臨時停車。(55.1.1)"))
        assertTrue(listOf("55") in matcher.lawRefs("違規臨時停車(第55條)"))
        assertTrue(listOf("56","1","2") in matcher.lawRefs("第56條第1項第2款"))
        assertTrue(listOf("31","6") in matcher.lawRefs("道交31-6未戴安全帽"))
    }
    @Test fun placeholdersSkipped() {
        assertEquals(5, matcher.options(categories).size)
        assertFalse(matcher.options(categories).any { it.text == "請選擇" })
    }
    @Test fun categoryWithLawNumber() {
        assertEquals("B", best("違規臨時停車(第55條)", categories))
        assertEquals("A", best("違規停車", categories))
    }
    @Test fun categoryFallbackToSubcategory() {
        assertEquals("C", matcher.bestMatch(listOf("交通違規檢舉專區", "交岔路口闖紅燈"), categories)?.option?.value)
    }
    @Test fun sub55WithLawCode() = assertEquals("1", best("在人行道、行人穿越道臨時停車。(但機車及騎樓不在此限)(55.1.1)", sub))
    @Test fun sub55TaipeiWordingWithoutCode() = assertEquals("1", best("汽車於人行道、行人穿越道臨時停車(但機車及騎樓不在此限)", sub))
    @Test fun sub55BusStop() = assertEquals("2", best("在交岔路口、公車站十公尺內或者消防車出入口五公尺內臨時停車", sub))
    @Test fun sub55DoubleParkingSlang() = assertEquals("5", best("雙排臨停", sub))
    @Test fun parkingVsTemporaryParking() {
        assertEquals("5611", best("在禁止臨時停車處所停車", parking))
        assertEquals("551", best("在設有禁止臨時停車標誌、標線處所臨時停車", parking))
    }
    @Test fun slangRedYellowLine() {
        assertEquals("5611", best("紅線停車", parking))
        assertEquals("5612", best("黃線停車", parking))
    }
    @Test fun disabledParking() = assertEquals("5615", best("違停身障車位", parking))
    @Test fun noMatchReturnsNone() {
        assertNull(best("超速行駛", parking))
        assertTrue("比對不到" in matcher.explainNoMatch(listOf("超速行駛"), parking))
    }
    @Test fun rankOrderAndCandidates() {
        val result = requireNotNull(matcher.bestMatch(listOf("併排停車"), parking))
        assertEquals("5614", result.option.value)
        assertEquals(result.option, result.candidates.first().option)
        val scores = matcher.rankOptions(listOf("併排停車"), parking).map { it.score }
        assertEquals(scores.sortedDescending(), scores)
    }
    @Test fun normalizeDistrict() {
        assertEquals("信義", matcher.normalizeDistrict("臺北市信義區"))
        assertEquals("彰化", matcher.normalizeDistrict("彰化市"))
        assertEquals("苓雅", matcher.normalizeDistrict("苓雅分局"))
        assertEquals("東", matcher.normalizeDistrict("東區"))
    }
    @Test fun districtEastVsDongshi() {
        val opts = texts("東勢區", "東區", "西區")
        assertEquals("東區", matcher.matchDistrict("東區", opts)?.option?.text)
        assertEquals("東區", matcher.matchDistrict("台中市東區", opts, "台中市")?.option?.text)
        assertEquals("東勢區", matcher.matchDistrict("東勢", opts)?.option?.text)
        assertNull(matcher.matchDistrict("東區", texts("東勢區","苓雅區")))
        assertEquals("苓雅區", matcher.matchDistrict("苓雅區中山二路", texts("東勢區","苓雅區"))?.option?.text)
    }
    @Test fun exactMatch() {
        val opts = listOf(Option("1","機車駕駛人，不依規定使用燈光。"),Option("2","汽車駕駛人，不依規定使用燈光。"))
        assertEquals("2", matcher.exactMatch("汽車駕駛人，不依規定使用燈光。",opts)?.option?.value)
        assertEquals("5", matcher.exactMatch("不當駕駛行為(燈光)(第42條)", listOf(Option("5","不當駕駛行為（燈光） （第42條）")))?.option?.value)
        assertEquals("2", matcher.exactMatch("汽車駕駛人，不依規定使用燈光",opts)?.option?.value)
        assertNull(matcher.exactMatch("駕駛人不依規定使用燈光。", opts))
        assertNull(matcher.exactMatch("汽車駕駛人不依規定使用燈光。", opts))
    }
    @Test fun exactMatchTrailingLawCode() {
        val cats = texts("請選擇違規類別","不當駕駛行為(燈光)(第42條)","不當駕駛行為(車道 、避讓)(第45條)",
            "不當駕駛行為(闖紅燈)(第53條、第53條之1)","不當駕駛行為(闖平交道)(第54條)")
        val subs = texts("請選擇","不按遵行之方向行駛。(45.1.1)","不依規定駛入來車道。(45.1.3)",
            "在多車道不依規定駕車。(45.1.4)","機車，不在規定車道行駛。(45.1.13)")
        assertEquals(cats[2], matcher.exactMatch("不當駕駛行為(車道、避讓)",cats)?.option)
        for (q in listOf("不依規定駛入來車道", "不依規定駛入來車道。",subs[2].text)) {
            assertEquals(subs[2], matcher.exactMatch(q,subs)?.option)
        }
        assertNull(matcher.exactMatch("駛入來車道",subs))
        assertEquals(cats[3], matcher.exactMatch("不當駕駛行為(闖紅燈)",cats)?.option)
        assertNull(matcher.exactMatch("違規停車",listOf(Option("1","違規停車。(56.1.1)"),Option("2","違規停車。(56.1.2)"))))
    }
    @Test fun reorderedCategory() = assertEquals("A", matcher.bestMatch(listOf("停車違規","在禁止臨時停車處所停車"),categories)?.option?.value)
    @Test fun districtVarious() {
        assertEquals("信義區", matcher.matchDistrict("臺北市信義區",texts("中正區","信義區","大安區"))?.option?.text)
        assertEquals("彰化市", matcher.matchDistrict("彰化市",texts("員林市","彰化市","鹿港鎮"))?.option?.text)
        assertEquals("12", matcher.matchDistrict("苓雅區",listOf(Option("12","苓雅區"),Option("13","前鎮區")))?.option?.value)
        assertNull(matcher.matchDistrict("板橋區",texts("中正區","信義區")))
    }
}
