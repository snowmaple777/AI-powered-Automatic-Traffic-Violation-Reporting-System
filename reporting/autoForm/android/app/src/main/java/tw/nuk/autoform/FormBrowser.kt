package tw.nuk.autoform

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.DocumentsContract
import android.provider.MediaStore
import android.webkit.*
import androidx.compose.runtime.*
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID

private val unmatchedLabels = mapOf("violation_district" to "違規行政區", "violation_location_district" to "地點行政區",
    "violation_category" to "違規事實大類", "violation_subcategory" to "違規事實小類")

@SuppressLint("SetJavaScriptEnabled")
class FormBrowser(context: Context, private val sites: Sites, private val input: Map<String, String>,
                  media: List<Media>, private val city: String = "高雄市",
                  private val pageSource: ((WebResourceRequest) -> WebResourceResponse)? = null) {
    private val driver = sites.drivers.getValue(city)
    private val regional = driver != "kaohsiung"
    private val config = sites.cityConfig(city)
    private val files = caseAttachments(driver, config, media)
    private val start = sites.startUrl(config, city)
    private val siteUri = Uri.parse(start)
    private val matcher = OptionMatcher(JSONObject(context.assetText("option_synonyms.json")))
    private val scripts = (listOf("_common") + (if (regional) listOf("_regional") else emptyList()) + driver)
        .joinToString("\n") { context.assetText("drivers/$it.js") }
    private val state = Workflow(config.optString("verification_timing", "before_submit"), files.size, files.size)
    private val handler = Handler(Looper.getMainLooper())
    // MATCH_PARENT：WRAP_CONTENT 的 WebView 會把 vh 算成 0，官網的彈出視窗會被壓扁。
    val view = WebView(context).apply {
        layoutParams = android.view.ViewGroup.LayoutParams(android.view.ViewGroup.LayoutParams.MATCH_PARENT, android.view.ViewGroup.LayoutParams.MATCH_PARENT)
    }
    private var document = 0
    private var loaded = false
    private var closed = false
    private var failed = false
    private var page = ""
    private var siteSubmitClicked = false
    private val advancedPages = mutableSetOf<String>()
    @Volatile private var ticket = ""
    // Published on the UI thread, read by the WebView interceptor. A page cannot choose a URI.
    private data class Attachment(val ticket: String, val media: Media)
    @Volatile private var attachment: Attachment? = null
    private val attachmentPath = "/.autoform/attachment/"
    private val viewContext = context.applicationContext
    private val resolver = viewContext.contentResolver
    private val missed = mutableStateListOf<String>()
    private val labels = unmatchedLabels.toMutableMap()
    val unmatched get() = missed.map { labels[it] ?: it }
    var step by mutableStateOf(Step.Start)
        private set
    var status by mutableStateOf("正在連線")
        private set
    var mayRetryUpload by mutableStateOf(false)
        private set
    val logs = mutableStateListOf<String>()

    private fun note(text: String) {
        status = text
        if (logs.lastOrNull() != text) {
            if (logs.size == 20) logs.removeAt(0)
            logs.add(text)
        }
    }
    private fun trusted(uri: Uri?): Boolean {
        if (uri == null) return false
        if (driver == "hsinchu_city" && uri.scheme == "https" && uri.encodedAuthority == "tra6.hccp.gov.tw") return true
        return uri.encodedAuthority == siteUri.encodedAuthority &&
            (uri.scheme == "https" || (uri.scheme == "http" && siteUri.scheme == "http"))
    }
    private fun valid(t: String) = t.isNotEmpty() && t == ticket
    private fun issue(): String {
        ticket = UUID.randomUUID().toString()
        attachment = null
        return ticket
    }

    init {
        WebView.setWebContentsDebuggingEnabled(false)
        view.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = false
            allowContentAccess = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            cacheMode = WebSettings.LOAD_NO_CACHE
            useWideViewPort = true
            loadWithOverviewMode = true
            setSupportZoom(true)
            builtInZoomControls = true
            displayZoomControls = false
            val version = Regex("Chrome/[0-9.]+").find(userAgentString)?.value ?: "Chrome/131.0.0.0"
            userAgentString = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) $version Safari/537.36"
        }
        CookieManager.getInstance().setAcceptThirdPartyCookies(view, false)
        view.importantForAutofill = android.view.View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
        view.addJavascriptInterface(Bridge(), "AutoFormBridge")
        view.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(v: WebView, req: WebResourceRequest) = !trusted(req.url)
            override fun shouldInterceptRequest(v: WebView, req: WebResourceRequest): WebResourceResponse? {
                if (!trusted(req.url)) {
                    // Static dependencies of the official verification widgets.
                    val uri = req.url
                    val widget = when (driver) {
                        "miaoli" -> uri.encodedAuthority == "remote.captcha.com" && uri.path == "/include.js"
                        "chiayi_city" -> uri.encodedAuthority == "unpkg.com" &&
                            uri.path.orEmpty().matches(Regex("/@microsoft/signalr@[^/]+/dist/browser/signalr\\.min\\.js"))
                        else -> false
                    }
                    return if (widget && !req.isForMainFrame && req.method == "GET" && uri.scheme == "https") null else denied()
                }
                if (req.url.path.orEmpty().startsWith(attachmentPath)) return localAttachment(req)
                return pageSource?.invoke(req)
            }
            override fun onPageStarted(v: WebView, url: String, icon: Bitmap?) {
                if (siteSubmitClicked) {
                    state.siteSubmitObserved()
                    step = state.step
                    note("等待網站回覆")
                }
                siteSubmitClicked = false
                document++
                ticket = ""
                attachment = null
                loaded = false
                failed = false
                page = ""
                mayRetryUpload = false
            }
            override fun onReceivedError(v: WebView, req: WebResourceRequest, err: WebResourceError) {
                if (req.isForMainFrame) loadError()
            }
            override fun onReceivedHttpError(v: WebView, req: WebResourceRequest, res: WebResourceResponse) {
                if (req.isForMainFrame) loadError()
            }
            override fun onPageFinished(v: WebView, url: String) {
                if (failed || closed || !trusted(Uri.parse(url)) || v.url != url) return
                loaded = true
                install()
            }
        }
        view.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(message: ConsoleMessage?) = true
        }
        view.loadUrl(start)
    }

    private fun denied() = WebResourceResponse("text/plain", "utf-8", 403, "Blocked",
        mapOf("Cache-Control" to "no-store"), null)

    // Serve this request locally, including on failure. Never forward an attachment URL to the network.
    private fun localAttachment(req: WebResourceRequest): WebResourceResponse {
        val grant = attachment ?: return denied()
        val uri = grant.media.uri
        if (req.isForMainFrame || req.method != "GET" || !valid(grant.ticket) ||
            req.url.encodedPath != attachmentPath + grant.ticket || uri.scheme != "content") return denied()
        return try {
            val headers = mutableMapOf("Cache-Control" to "no-store")
            val isDocument = DocumentsContract.isDocumentUri(viewContext, uri)
            val modifiedColumn = if (isDocument) DocumentsContract.Document.COLUMN_LAST_MODIFIED else MediaStore.MediaColumns.DATE_MODIFIED
            val modified = runCatching {
                resolver.query(uri, arrayOf(modifiedColumn), null, null, null)?.use { cursor ->
                    if (cursor.moveToFirst()) cursor.getLong(0) else 0L
                }
            }.getOrNull()
            if (modified != null && modified > 0) {
                headers["X-File-Modified"] = (if (isDocument) modified else modified * 1000).toString()
            }
            val type = resolver.getType(uri) ?: "application/octet-stream"
            val stream = resolver.openInputStream(uri) ?: return denied()
            WebResourceResponse(type, null, 200, "OK", headers, stream)
        } catch (_: Exception) { denied() }
    }

    private fun loadError() {
        loaded = false
        failed = true
        ticket = ""
        note("網頁載入失敗，請檢查網路後重試。")
    }
    private fun js(code: String, callback: ((String) -> Unit)? = null) {
        if (closed || !loaded || !trusted(view.url?.let(Uri::parse))) return
        val uri = Uri.parse(view.url)
        val guard = JSONObject.quote("${uri.scheme}://${uri.encodedAuthority}")
        view.evaluateJavascript("(()=>{if(location.origin!==$guard || window!==top)return; $code })()") {
            if (!closed) callback?.invoke(it)
        }
    }
    private fun install() {
        siteSubmitClicked = false
        val gen = ++document
        ticket = ""
        attachment = null
        val selectors = config.getJSONObject("selectors")
        val red = JSONArray(missed)
        js("window.AutoFormSelectors=$selectors;\n$scripts\nwindow.__afDocument=$gen;window.__afTicket='';$red.forEach(k=>AutoFormTools.highlight(AutoFormSelectors[k]));") {
            if (gen == document) detect(gen, true)
        }
    }

    private fun detect(gen: Int, first: Boolean) {
        if (!loaded || closed || gen != document) return
        js("return window.AutoFormDriver?.detectStep(location.href,document) || 'unknown';") { raw ->
            if (!loaded || gen != document) return@js
            val detected = raw.trim('"')
            if (first || detected != page) {
                if (regional && first && page.isEmpty() && detected == "fill") state.formReloaded()
                page = detected
                state.arrived(when {
                    regional && page.startsWith("gate") -> "verify"
                    regional && page == "fill" -> "verified"
                    else -> page
                })
                drive()
            }
            handler.postDelayed({ detect(gen, false) }, 700)
        }
    }

    private fun drive() {
        step = state.step
        val t = issue()
        if (page == "maintenance") { note("官網目前顯示停機維護，請稍後再開啟填表頁。"); return }
        if (regional && (page.startsWith("gate") || page.startsWith("intro"))) {
            note(when (page) {
                "gateDraft" -> "請在官網選擇草稿或開始新案件。"
                "gateEmail" -> "請在官網完成信箱驗證，案件欄位開放後會繼續填表。"
                "introReporter" -> "正在填寫通訊資料，請核對官網欄位。"
                else -> "請在官網完成頁面上的人工驗證，進入案件表單後會繼續。"
            })
            execute("prepare", t)
            return
        }
        if (regional && page == "review") { note("請在官網核對資料與附件，完成最後驗證及送出。"); return }
        if (step == Step.Done) { note("已離開填表頁，請依官網指示確認信件與受理狀態。"); return }
        if (step == Step.Pending) { note("等待網站回覆，請查看頁面結果。"); return }
        if (page in setOf("unknown","done")) { note("目前頁面無法辨識，請核對網站狀態。"); return }
        if (step == Step.Verify) {
            if (driver == "hsinchu_county" && page == "fill" && missed.isEmpty() && advancedPages.add("review")) {
                execute("review", t)
                return
            }
            val red = JSONArray(missed)
            js("window.__afTicket=" + JSONObject.quote(t) + ";$red.forEach(k=>AutoFormTools.highlight(AutoFormSelectors[k]));")
            note("自動填表完成。請在官網核對資料及附件，完成驗證並送出。")
            return
        }
        if (step == Step.EarlyVerify) {
            note("請在官網完成驗證，通過後將繼續填表。")
            return
        }
        if (step == Step.Start && page != "start" || step != Step.Start && page !in setOf("fill","verified")) {
            note("目前網址與填表進度不符，請檢查頁面。")
            return
        }
        val label = if (step in setOf(Step.Upload, Step.Receipt)) "附件 " + (state.index + 1) + "/" + state.limit else step.label
        note(label)
        execute(step.action, t)
    }

    private fun execute(action: String, t: String = issue()) {
        val ctx = JSONObject().put("ticket", t).put("config", config).put("catalog", sites.catalog)
            .put("mediaCount", state.limit).put("uploadIndex", state.index).put("baseline", state.baseline)
        ctx.put("city", city)
        if (driver == "hsinchu_county" && action == "prepare") ctx.put("advance", "introReporter" !in advancedPages)
        if (action in setOf("start", "prepare", "fill", "cascade", "details")) ctx.put("input_data", JSONObject(input))
        if (action == "upload") {
            val item = files[state.index]
            attachment = Attachment(t, item)
            ctx.put("filename", item.name)
            ctx.put("video", item.video).put("typeIndex", files.take(state.index).count { it.video == item.video })
            ctx.put("lastOfType", files.drop(state.index + 1).none { it.video == item.video })
        }
        val code = """
            if(window.__afDocument!==$document)return;
            const driver=window.AutoFormDriver;
            if(!driver || driver.detectStep(location.href,document)!==${JSONObject.quote(page)})return;
            const ctx=$ctx;
            window.__afTicket=ctx.ticket;
            ctx.active=()=>window.__afTicket===ctx.ticket;
            const report=(name,data)=>{if(ctx.active())AutoForm.stepDone(name,JSON.stringify({...data,ticket:ctx.ticket}));};
            Promise.resolve().then(()=>driver.steps[${JSONObject.quote(action)}](ctx))
                .then(data=>report(${JSONObject.quote(action)},data||{})).catch(()=>report('error',{}));
        """.trimIndent()
        js(code)
        handler.postDelayed({
            if (valid(t)) note(if (action == "upload") "正在讀取案件附件…" else "仍在等待網站，可檢查頁面並重新注入本步驟。")
        }, 20000)
    }

    private inner class Bridge {
        @JavascriptInterface fun log(code: String) {
            if (code == "reading-file") handler.post { if (!closed && step == Step.Upload) note("正在讀取案件附件") }
        }
        @JavascriptInterface fun requestFile(t: String): String =
            if (valid(t) && attachment?.ticket == t) attachmentPath + t else ""
        @JavascriptInterface fun match(t: String, mode: String, query: String, raw: String): Int {
            if (!valid(t)) return -1
            return runCatching {
                val arr = JSONArray(raw)
                val opts = List(arr.length()) { i -> arr.getJSONObject(i).let { Option(it.getString("value"), it.getString("text")) } }
                val qs = if (query.startsWith("[")) JSONArray(query).strings() else listOf(JSONArray("[$query]").getString(0))
                val result = when (mode) {
                    "exact" -> matcher.exactMatch(qs.first(), opts)
                    "district" -> matcher.matchDistrict(qs.first(), opts, city)
                    else -> matcher.bestMatch(qs, opts, if (mode == "violation") 0.6 else 0.55)
                }
                opts.indexOf(result?.option)
            }.getOrDefault(-1)
        }
        @JavascriptInterface fun reportUnmatched(json: String) = post(json) {
            val key = it.optString("key")
            if (config.getJSONObject("selectors").has(key) || (regional && key in setOf("violation_items","entrance_district","reporter_road","extra_date"))) {
                if (it.optBoolean("resolved")) missed.remove(key)
                else {
                    if (regional) labels[key] = it.optString("label").ifBlank { labels[key] ?: key }
                    if (key !in missed) missed.add(key)
                }
            }
        }
        @JavascriptInterface fun stepDone(name: String, json: String) = post(json) { result ->
            when {
                step == Step.Verify && name in setOf("manualSubmit", "otherClick") -> {
                    siteSubmitClicked = name == "manualSubmit"
                }
                name == "error" -> {
                    ticket = ""
                    note(if (step == Step.Upload) "無法載入第 ${state.index + 1} 個附件，請確認原檔可讀後重試。"
                        else "本步驟未完成，請檢查頁面並重試。")
                }
                name == "prepare" || name == "review" -> {
                    ticket = ""
                    if (page == "introReporter") note("請核對通訊資料，依官網指示前往下一步。")
                    if (name == "review") note("請核對附件及官網資料，依網站指示前往最後確認頁。")
                }
                name == "advancingReporter" && driver == "hsinchu_county" && page == "introReporter" -> advancedPages.add(page)
                regional && name == "upload" && step == Step.Upload && result.optBoolean("attached") -> {
                    state.fileAttached()
                    drive()
                }
                !regional && name == "upload" && step == Step.Upload && state.beginUpload(result.optInt("count", -1)) -> {
                    step = state.step
                    execute("uploadPost")
                }
                name == "receipt" && step == Step.Receipt -> {
                    val count = result.optInt("count", -1)
                    if (state.receipt(count)) { mayRetryUpload = false; drive() }
                    else {
                        ticket = ""
                        mayRetryUpload = count == state.baseline
                        note("官網尚未確認新增附件，請核對已上傳件數。")
                    }
                }
                name == "uploadPost" && step == Step.Receipt -> drive()
                name in setOf("fill","cascade","details") && name == state.step.action -> { state.next(); drive() }
            }
        }
        private fun post(raw: String, handle: (JSONObject) -> Unit) {
            val data = runCatching { JSONObject(raw) }.getOrNull() ?: return
            handler.post { if (!closed && valid(data.optString("ticket"))) handle(data) }
        }
    }

    fun retry() {
        if (closed || step == Step.Done) return
        if (failed) {
            if (state.step == Step.Pending || state.step == Step.Receipt) note("傳送結果不明，請先核對官網回覆。")
            else view.loadUrl(view.url?.takeIf { trusted(Uri.parse(it)) } ?: start)
        } else if (loaded && step != Step.Pending) install()
    }
    fun retryUpload() {
        if (!mayRetryUpload) return
        mayRetryUpload = false
        state.retryUnsentUpload()
        drive()
    }
    fun close() {
        closed = true
        ticket = ""
        handler.removeCallbacksAndMessages(null)
        view.removeJavascriptInterface("AutoFormBridge")
        view.stopLoading()
        view.destroy()
    }
}
