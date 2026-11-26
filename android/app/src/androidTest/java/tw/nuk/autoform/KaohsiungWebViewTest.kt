package tw.nuk.autoform

import android.content.ContentValues
import android.net.Uri
import android.os.SystemClock
import android.provider.MediaStore
import android.webkit.WebResourceResponse
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.SdkSuppress
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Exercises automatic delivery of selected content URIs. The site and all uploads stay local. */
@RunWith(AndroidJUnit4::class)
class KaohsiungWebViewTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val posts = AtomicInteger()
    private lateinit var browser: FormBrowser

    private fun eval(js: String): String {
        val latch = CountDownLatch(1)
        var value = ""
        instrumentation.runOnMainSync {
            browser.view.evaluateJavascript(js) { value = it; latch.countDown() }
        }
        assertTrue(latch.await(5, TimeUnit.SECONDS))
        return value
    }
    private fun awaitState(predicate: () -> Boolean) {
        val limit = SystemClock.elapsedRealtime() + 50000
        while (SystemClock.elapsedRealtime() < limit) {
            var ok = false
            instrumentation.runOnMainSync { ok = predicate() }
            if (ok) return
            SystemClock.sleep(100)
        }
        val dom = eval("""(()=>{const f=document.querySelector('input[type=file]');return [f?.files?.length,document.readyState,window.innerWidth,document.body?.textContent.includes("目前已上傳")];})()""")
        fail("WebView timeout: " + browser.step + " / " + browser.logs.joinToString() + " / " + dom)
    }
    private fun fixture(uri: Uri, sites: Sites): String {
        val s = sites.config.getJSONObject("selectors")
        fun id(key: String) = s.getString(key).removePrefix("#")
        fun element(key: String, type: String = "text") = "<input id='${id(key)}' type='$type'>"
        if (uri.path == "/Statement.aspx") {
            val boxes = s.getJSONArray("agreement_checkboxes").strings().joinToString("") {
                "<input type='checkbox' id='${it.drop(1)}'>"
            }
            return boxes + "<button id='${id("agreement_submit")}' onclick=\"if(document.querySelectorAll(':checked').length===4)location.href='/Mail.aspx'\">開始</button>"
        }
        if (uri.path != "/Mail.aspx") return ""
        val reload = uri.getQueryParameter("group") != null
        val count = uri.getQueryParameter("count")?.toInt() ?: 0
        val entry = sites.config.getJSONObject("violation_map").getJSONObject("橫跨雙黃線")
        return buildString {
            append("<meta name='viewport' content='width=device-width,initial-scale=1'><body>")
            for (key in profileFields.keys + listOf("violation_date","violation_plate_part1","violation_plate_part2","violation_location_address","violation_description")) append(element(key))
            for (key in listOf("reporter_nationality_domestic","reporter_gender_male","reporter_gender_female","car_type_auto","car_type_heavy_moto","car_type_light_moto")) append(element(key,"checkbox"))
            append("<input type='range' name='hours' max='23'><input type='range' name='minutes' max='59'>")
            for (key in listOf("violation_district","violation_location_district")) append("<select id='${id(key)}'><option>請選擇</option><option>苓雅區</option></select>")
            append("<select id='${id("violation_category")}' onchange=\"location.href='/Mail.aspx?group=1'\"><option>請選擇</option><option ${if(reload) "selected" else ""}>${entry.getString("category")}</option></select>")
            append("<select id='${id("violation_subcategory")}'><option>請選擇</option>")
            if (reload) append("<option>${entry.getString("subcategory")}</option>")
            append("</select><table><tr><td>目前已上傳 <b>$count</b> 個檔案")
            append(element("file_upload_input","file"))
            val ext = if (count == 1) "mp4" else "jpg"
            val mime = if (count == 1) "video/mp4" else "image/jpeg"
            append("<button id='${id("file_upload_submit")}' onclick=\"const f=document.querySelector('[type=file]').files[0];if(f?.name==='fixture-$count.$ext' && f.type==='$mime')f.text().then(t=>{if(t==='case-$count')location.href='/Mail.aspx?group=1&amp;count=${count+1}'})\">上傳</button></td></tr></table>")
            append("<input id='${id("captcha_input")}' maxlength='4'><button id='${id("submit_btn")}' onclick=\"location.href='/unexpected-submit'\">送出</button>")
            // Mirror the site's ViewState restoration. File inputs and outline styles never survive a reload.
            append("""<script>
                for(const e of document.querySelectorAll('input[id]:not([type=file]),select[id]')) {
                    const key=e.id, v=sessionStorage.getItem(key);
                    if(v!==null){if(e.type==='checkbox')e.checked=v==='true';else e.value=v;}
                    e.addEventListener('input',()=>sessionStorage.setItem(key,e.type==='checkbox'?e.checked:e.value));
                }
            </script>""")
        }
    }

    @Test @SdkSuppress(minSdkVersion = 29)
    fun fullPostbacksThreeAttachmentsAndManualVerification() {
        val context = instrumentation.targetContext
        val data = mapOf("district" to "不存在行政區", "violation_date" to "20260506", "violation_time" to "08:37",
            "reporter_id" to "x200000000", "license_plate" to "ABC-1234", "car_type" to "機車",
            "violation_subcategory" to "跨越雙黃線", "violation_location" to "測試地點", "violation_description" to "測試敘述")
        val media = mutableListOf<Media>()
        try {
            repeat(4) { index ->
                val video = index == 1
                val name = "fixture-$index." + if (video) "mp4" else "jpg"
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                    put(MediaStore.MediaColumns.MIME_TYPE, if (video) "video/mp4" else "image/jpeg")
                }
                val collection = if (video) MediaStore.Video.Media.EXTERNAL_CONTENT_URI else MediaStore.Images.Media.EXTERNAL_CONTENT_URI
                val uri = requireNotNull(context.contentResolver.insert(collection, values))
                media.add(Media(uri, name, video))
                context.contentResolver.openOutputStream(uri)!!.use { it.write("case-$index".toByteArray()) }
            }
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                scenario.onActivity { activity ->
                    val sites = Sites(activity)
                    browser = FormBrowser(activity, sites, data, media) { request ->
                        if (request.url.path == "/unexpected-submit") posts.incrementAndGet()
                        WebResourceResponse("text/html", "utf-8", fixture(request.url, sites).byteInputStream())
                    }
                    activity.setContentView(browser.view)
                }
                awaitState { browser.step == Step.Verify }
                assertEquals("true", eval("""(()=>{
                    const s=AutoFormSelectors, el=k=>document.querySelector(s[k]);
                    return el("reporter_id").value==="X200000000" && el("reporter_gender_female").checked &&
                        el("violation_date").value==="2026-05-06 08:37" && el("violation_plate_part2").value==="1234" &&
                        el("car_type_light_moto").checked && el("violation_subcategory").selectedIndex===1 &&
                        el("violation_location_address").value==="測試地點" && el("violation_description").value==="測試敘述" &&
                        el("violation_district").style.outline.includes("red") && location.search.includes("count=3");
                })()"""))
                assertEquals("\"\"", eval("document.querySelector(AutoFormSelectors.captcha_input).value"))
                assertEquals(Step.Verify, browser.step)
                assertEquals(0, posts.get())
                scenario.onActivity { it.setContentView(android.widget.FrameLayout(it)); browser.close() }
            }
        } finally {
            media.forEach { context.contentResolver.delete(it.uri, null, null) }
        }
    }
}
