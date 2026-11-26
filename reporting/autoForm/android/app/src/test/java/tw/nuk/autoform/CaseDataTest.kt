package tw.nuk.autoform

import org.junit.Assert.*
import org.junit.Test

class CaseDataTest {
    @Test fun importsUtf8FieldsAndDistrictFallback() {
        val data = parseInfo(sequenceOf("無分隔符", "違規時間: 08:05", "違規行政區:  ", "交通違規地點行政區: 苓雅區", "違規事實敘述:一", "違規事實敘述: 二:三"))
        assertEquals("08:05", data["violation_time"])
        assertEquals("苓雅區", data["district"])
        assertEquals("二:三", data["violation_description"])
        assertEquals("", data["reporter_id"].orEmpty())
        assertEquals("左營區", parseInfo(sequenceOf("違規行政區(必填): 左營區"))["district"])
    }

    @Test fun recommendationFollowsDesktopPriority() {
        assertEquals("高雄市", recommendCity("三民區"))
        assertEquals("台南市", recommendCity("東區"))
        assertEquals("新竹縣", recommendCity("竹北"))
        assertNull(recommendCity("未知"))
    }
}
