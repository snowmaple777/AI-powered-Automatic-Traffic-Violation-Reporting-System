package tw.nuk.autoform

import android.content.Intent
import android.os.SystemClock
import android.view.MotionEvent
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.TimeUnit
import java.util.concurrent.CountDownLatch

@RunWith(AndroidJUnit4::class)
class ProjectWorkflowTest {
    private val inst = InstrumentationRegistry.getInstrumentation()
    private lateinit var web: WebView
    private fun findWeb(view: View): WebView? {
        if (view is WebView) return view
        if (view is ViewGroup) for (i in 0 until view.childCount) findWeb(view.getChildAt(i))?.let { return it }
        return null
    }
    private fun js(code:String):String {
        val done=CountDownLatch(1)
        var value=""
        inst.runOnMainSync { web.evaluateJavascript(code) { value=it; done.countDown() } }
        assertTrue(done.await(8,TimeUnit.SECONDS))
        return value
    }
    private fun await(condition:String, seconds:Int=45) {
        val end=SystemClock.elapsedRealtime()+seconds*1000
        while(SystemClock.elapsedRealtime()<end) {
            if(js(condition)=="true") return
            SystemClock.sleep(250)
        }
        fail("Workflow timeout: $condition / " + js("JSON.stringify([location.pathname,document.readyState,typeof appStarted,document.querySelector('#toast')?.innerText,typeof state==='undefined'?null:state.job?.status])"))
    }

    @Test fun loginUploadGpsReviewAndNativeCase() {
        val port=InstrumentationRegistry.getArguments().getString("projectTestPort")
        assumeTrue("Requires the isolated UI fixture",port!=null)
        val host=InstrumentationRegistry.getArguments().getString("projectTestHost") ?: "10.0.2.2"
        val target=Intent(inst.targetContext,ProjectActivity::class.java).putExtra("address","http://$host:$port")
        ActivityScenario.launch<ProjectActivity>(target).use { scenario ->
            scenario.onActivity { web=requireNotNull(findWeb(it.window.decorView)) }
            await("typeof googleLogin==='function'")
            js("sessionStorage.setItem('fixtureReload','1');tokenStore.set(null);location.replace('/?new=1')")
            await("location.search==='?new=1' && typeof googleLogin==='function' && !tokenStore.get()")
            js("googleLogin('fixture@example.invalid','測試')")
            await("appStarted && state.cities.length>0")
            js("""(async()=>{const response=await fetch('/files/clips/my/fixture.mp4');
                if(response.ok) await uploadVideo(new File([await response.blob()],'fixture.mp4',{type:'video/mp4'}))})()""")
            await("!!state.video?.video_id")
            js("""(()=>{const dt=new DataTransfer();dt.items.add(new File(['lat,lon,time\n22.63,120.30,2026-11-20T08:00:00\n22.631,120.301,2026-11-20T08:00:01'],'track.csv'));
                const el=document.querySelector('#gps-input');el.files=dt.files;el.dispatchEvent(new Event('change',{bubbles:true}))})()""")
            await("!!state.gps.track?.length")
            if (InstrumentationRegistry.getArguments().getString("projectRealModel")=="true") {
                js("setMode('remote');document.querySelector('#max-seconds').value='1'")
                await("state.health?.online===true")
                js("window.fixturePrevious=state.job?.id;startAnalysis()")
                await("state.job?.id!==window.fixturePrevious && state.job?.analyzer==='remote' && ['error','done'].includes(state.job.status)",240)
                assertEquals("\"done\"",js("state.job.status"))
            }
            js("window.fixturePrevious=state.job?.id;setMode('simulated');startAnalysis()")
            await("state.job?.id!==window.fixturePrevious && state.job?.analyzer==='simulated' && state.job?.status==='done' && state.job.violations.length>0",100)
            js("""(()=>{const event=state.job.violations[0];openCaseForm(event);
                const form=document.querySelector('#case-form');form.elements.city.value='臺中市';
                form.elements.plate.value='TST-1234';form.elements.location.value='測試路口';
                form.dispatchEvent(new Event('input'));document.querySelector('#btn-make-case').click()})()""")
            await("!!document.querySelector('a[href^=\"autoform://case/\"]')")
            val link="document.querySelector('a[href^=\"autoform://case/\"]')"
            js("$link.scrollIntoView({block:'center'})")
            SystemClock.sleep(400)
            val point=js("(()=>{const r=$link.getBoundingClientRect();return [(r.left+r.width/2)/innerWidth,(r.top+r.height/2)/innerHeight]})()")
            val fractions=org.json.JSONArray(point)
            val monitor=inst.addMonitor(CaseActivity::class.java.name,null,false)
            try {
                val pos=IntArray(2)
                var x=0f;var y=0f
                inst.runOnMainSync { web.getLocationOnScreen(pos);x=pos[0]+(web.width*fractions.getDouble(0)).toFloat();y=pos[1]+(web.height*fractions.getDouble(1)).toFloat() }
                val now=SystemClock.uptimeMillis()
                for (action in listOf(MotionEvent.ACTION_DOWN,MotionEvent.ACTION_UP)) {
                    val event=MotionEvent.obtain(now,SystemClock.uptimeMillis(),action,x,y,0)
                    try { inst.sendPointerSync(event) } finally { event.recycle() }
                }
                val activity=inst.waitForMonitorWithTimeout(monitor,15000) as? CaseActivity
                assertNotNull("第 5 步未開啟手機填表",activity)
                val case=activity!!.intent
                assertEquals("台中市",case.getStringExtra("city"))
                assertEquals("TST-1234",case.getBundleExtra("fields")?.getString("license_plate"))
                val evidence=case.getStringArrayListExtra("uris")!!.map(android.net.Uri::parse)
                assertEquals(setOf("evidence.jpg","evidence.mp4"),evidence.map { it.lastPathSegment }.toSet())
                evidence.forEach { uri -> assertTrue(inst.targetContext.contentResolver.openInputStream(uri)!!.use { it.read()>=0 }) }
                inst.runOnMainSync { activity.finish() }
                await("!!document.querySelector('a[href^=\"autoform://case/\"]')")
            } finally { inst.removeMonitor(monitor) }
        }
    }
}
