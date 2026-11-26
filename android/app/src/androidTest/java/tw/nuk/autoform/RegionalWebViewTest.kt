package tw.nuk.autoform

import android.content.ContentValues
import android.os.SystemClock
import android.provider.MediaStore
import android.webkit.WebResourceResponse
import android.widget.FrameLayout
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.SdkSuppress
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class RegionalWebViewTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context get() = instrumentation.targetContext
    private lateinit var browser: FormBrowser

    private fun eval(code: String): String {
        val latch = CountDownLatch(1)
        var result = ""
        instrumentation.runOnMainSync {
            browser.view.evaluateJavascript(code) { result = it; latch.countDown() }
        }
        assertTrue(latch.await(5, TimeUnit.SECONDS))
        return result
    }

    private fun waitFor(code: String) {
        val until = SystemClock.elapsedRealtime() + 45000
        while (SystemClock.elapsedRealtime() < until) {
            if (eval(code) == "true") return
            SystemClock.sleep(100)
        }
        fail("未抵達測試步驟 " + browser.status)
    }

    private fun withPage(city: String, html: String, media: List<Media> = emptyList(), check: () -> Unit) {
        val input = mapOf("reporter_email" to "fixture@example.invalid", "license_plate" to "ABC-1234",
            "district" to "東區", "violation_subcategory" to "闖紅燈", "violation_description" to "測試案件")
        ActivityScenario.launch(MainActivity::class.java).use { scenario ->
            scenario.onActivity { activity ->
                browser = FormBrowser(activity, Sites(activity), input, media, city) {
                    WebResourceResponse("text/html", "utf-8", html.byteInputStream())
                }
                activity.setContentView(browser.view)
            }
            try { check() } finally {
                scenario.onActivity { it.setContentView(FrameLayout(it)); browser.close() }
            }
        }
    }

    @Test fun allCityRoutesHavePackagedConfigsAndDrivers() {
        val sites = Sites(context)
        assertEquals(20, sites.drivers.size)
        for ((city, driver) in sites.drivers) {
            assertTrue(city, city in sites.urls)
            assertTrue(city, sites.startUrl(sites.cityConfig(city),city).startsWith("http"))
            assertTrue(city, context.assetText("drivers/$driver.js").isNotBlank())
        }
        assertEquals(sites.urls["台北市"], sites.startUrl(sites.cityConfig("台北市"),"台北市"))
    }

    @Test @SdkSuppress(minSdkVersion = 29)
    fun chiayiCityWaitsForWebsiteAndBatchesAttachmentsByType() {
        val html = """
            <input type="email"><input id="otpCode"><button onclick="window.sent++">發送驗證碼</button>
            <section hidden id="caseFields">
                <input placeholder="請輸入車牌號碼"><input type="radio" value="汽車"><input id="agreement_2078" type="checkbox">
                <input id="image-input-0" type="file" multiple onchange="window.imageChanges++">
                <input id="video-input-0" type="file" multiple onchange="window.videoChanges++">
                <form><button type="submit" onclick="window.sent++;return false">確認送出</button></form>
            </section><script>window.sent=window.imageChanges=window.videoChanges=0</script>
        """.trimIndent()
        val files = mutableListOf<Media>()
        try {
            for ((i, video) in listOf(false,true,false,true,false,true,false).withIndex()) {
                val name = "regional-$i." + if (video) "mp4" else "jpg"
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME,name)
                    put(MediaStore.MediaColumns.MIME_TYPE,if (video) "video/mp4" else "image/jpeg")
                }
                val collection = if (video) MediaStore.Video.Media.EXTERNAL_CONTENT_URI else MediaStore.Images.Media.EXTERNAL_CONTENT_URI
                val uri = requireNotNull(context.contentResolver.insert(collection,values))
                files.add(Media(uri,name,video))
                context.contentResolver.openOutputStream(uri)!!.use { it.write(byteArrayOf(i.toByte())) }
            }
            withPage("嘉義市",html,files) {
                waitFor("document.querySelector('input[type=email]')?.value==='fixture@example.invalid'")
                assertEquals("true",eval("otpCode.value==='' && window.sent===0 && document.querySelector('[type=file]').files.length===0"))
                // Simulate the official SPA unlocking its form, without navigation or an app confirmation.
                eval("caseFields.hidden=false")
                waitFor("window.videoChanges===1 && window.imageChanges===1")
                assertEquals("true",eval("""(()=>{const names=k=>Array.from(document.getElementById(k).files,f=>f.name).join(',');
                    return names('image-input-0')==='regional-0.jpg,regional-2.jpg,regional-4.jpg' &&
                    names('video-input-0')==='regional-1.mp4,regional-3.mp4' &&
                    document.querySelector('[placeholder]').value==='ABC-1234' && otpCode.value==='' && window.sent===0})()"""))
            }
        } finally { files.forEach { context.contentResolver.delete(it.uri,null,null) } }
    }

    @Test fun taipeiWaitsForEmailAndResumesWhenFormAppears() {
        val html = """<input id="sPub_id"><input id="email"><button onclick="window.sent++">發送認證信</button>
            <section hidden id="caseFields"><textarea id="sRuldec"></textarea><input id="input-100"><input id="input-103">
            <input id="input-108" readonly onclick="openMenu(this,'東區')">
            <input id="input-148" readonly onclick="openMenu(this,'闖紅燈(第53條)')">
            <input type="checkbox" id="input-157"><div class="v-menu__content" id="options"></div>
            <button onclick="window.sent++">送出</button></section><script>window.sent=0</script>"""
        val menu = """<script>function openMenu(field,label){
            const item=document.createElement('div'); item.className='v-list-item'; item.textContent=label;
            item.onclick=()=>{field.value=label;item.remove()}; options.replaceChildren(item)
        }</script>"""
        withPage("台北市",html + menu) {
            waitFor("document.querySelector('#email')?.value==='fixture@example.invalid'")
            assertEquals("true",eval("sRuldec.value==='' && window.sent===0"))
            eval("caseFields.hidden=false")
            waitFor("document.querySelector('#sRuldec')?.value==='測試案件'")
            assertEquals("true",eval("document.getElementById('input-100').value==='ABC' && window.sent===0"))
            assertEquals("true",eval("""document.getElementById('input-108').value==='東區' &&
                document.getElementById('input-148').value==='闖紅燈(第53條)' && document.getElementById('input-157').checked"""))
        }
    }

    @Test fun taitungConfiguredFormFillsAndStopsAtCaptcha() {
        val html = """<input id="name"><input id="email"><input id="car"><input id="chkint">
            <select id="area"><option>請選擇</option><option>東區</option></select>
            <select id="subject"><option>請選擇</option><option>闖紅燈(第53條)</option></select>
            <textarea id="content1"></textarea><button id="save" onclick="window.sent++">送出</button>
            <script>window.sent=0</script>"""
        withPage("台東縣",html) {
            waitFor("document.querySelector('#content1')?.value==='測試案件'")
            assertEquals("true",eval("car.value==='ABC-1234' && area.selectedIndex===1 && subject.selectedIndex===1 && chkint.value==='' && window.sent===0"))
        }
    }
}
