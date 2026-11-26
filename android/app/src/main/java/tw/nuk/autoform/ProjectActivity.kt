package tw.nuk.autoform

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.webkit.*
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.lifecycleScope
import androidx.webkit.ProxyConfig
import androidx.webkit.ProxyController
import androidx.webkit.WebViewFeature
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class ProjectActivity : ComponentActivity() {
    private lateinit var server: ProjectConnection
    private lateinit var web: WebView
    private var status by mutableStateOf("")
    private var busy by mutableStateOf(false)
    private var connected by mutableStateOf(false)
    private var selected: ValueCallback<Array<Uri>>? = null
    private val picker = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        selected?.onReceiveValue(uri?.takeIf { it.scheme == "content" }?.let { arrayOf(it) })
        selected = null
    }
    private var downloadPath: String? = null
    private val saveDocument = registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { destination ->
        val path = downloadPath
        downloadPath = null
        if (path != null && destination != null) {
            val token = sessionToken() ?: return@registerForActivityResult
            task("正在儲存檔案") {
                withContext(Dispatchers.IO) {
                    requireNotNull(contentResolver.openOutputStream(destination,"wt")).use { server.export(path,token,it) }
                }
                status = "檔案已儲存"
            }
        }
    }

    private var manual by mutableStateOf("")
    private val lanPermission = registerForActivityResult(ActivityResultContracts.RequestPermission()) { allowed ->
        if (allowed) connect() else status = "請在系統設定允許本 App 存取區域網路，再按「重新搜尋」。"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // 測試或舊流程可指定網址；一般從桌面開啟時自動在同一個 Wi-Fi 尋找分析電腦。
        intent.getStringExtra("address")?.let { runCatching { ProjectConnection.parse(it) }.getOrNull() }?.let { server = it }
        web = makeWebView()
        setContent { MaterialTheme { Surface {
            Column(Modifier.fillMaxSize().safeDrawingPadding().imePadding()) {
                if (!connected) {
                    Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        if (status.isNotEmpty()) Text(status)
                        if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
                        if (!busy) {
                            Button(onClick = { connect() }) { Text("重新搜尋") }
                            // 只有自動搜尋失敗時才出現：例如電腦不在同一個網段。
                            OutlinedTextField(manual, { manual = it }, Modifier.fillMaxWidth(), singleLine = true,
                                label = { Text("或手動輸入分析電腦網址") }, placeholder = { Text("http://192.168.1.10:8765") })
                            TextButton(enabled = manual.isNotBlank(), onClick = {
                                runCatching { ProjectConnection.parse(manual) }.getOrNull()?.let { server = it; connect(false) }
                                    ?: run { status = "網址格式有誤，請輸入手機展示模式顯示的 http:// 網址。" }
                            }) { Text("連線") }
                        }
                    }
                } else if (status.isNotEmpty() || busy) {
                    Column(Modifier.padding(horizontal = 12.dp, vertical = 4.dp)) {
                        if (status.isNotEmpty()) Text(status)
                        if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
                    }
                }
                AndroidView(factory={web},modifier=Modifier.weight(1f))
            }
            BackHandler { if (web.canGoBack() && !busy) web.goBack() else finish() }
        } } }
        connect(!::server.isInitialized)
    }

    private fun connect(search: Boolean = true) {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE_REVERSE_BYPASS) ||
            !WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE)) {
            status = "請先從 Play 商店更新 Android System WebView 或 Chrome，再重新連線。"
            return
        }
        val lan = "android.permission.ACCESS_LOCAL_NETWORK"
        if (Build.VERSION.SDK_INT >= 37 && checkSelfPermission(lan) != PackageManager.PERMISSION_GRANTED) {
            lanPermission.launch(lan); return
        }
        task(if (search) "正在同一個 Wi-Fi 尋找分析電腦…" else "正在連線到分析電腦") {
            val found = withContext(Dispatchers.IO) {
                if (search) Discovery.find(this@ProjectActivity)
                else server.also { it.checkService() }
            } ?: throw ProjectError("找不到分析電腦。請確認電腦已開啟「手機展示模式」，且手機與電腦連同一個 Wi-Fi。")
            server = found
            Discovery.remember(this@ProjectActivity, found.address)
            val server = found
            // Only this project's reserved origin is routed to the user's LAN computer.
            // All official reporting sites continue to use their normal network connection.
            val config = ProxyConfig.Builder().addProxyRule(server.address)
                .addBypassRule(server.webHost).setReverseBypassEnabled(true).build()
            ProxyController.getInstance().setProxyOverride(config,mainExecutorCompat()) {
                if (!isDestroyed && !isFinishing) { connected=true; status=""; web.loadUrl(server.origin + "/") }
            }
        }
    }
    private fun mainExecutorCompat() = java.util.concurrent.Executor { runOnUiThread(it) }
    private fun task(label: String, work: suspend ()->Unit) {
        if (busy) return
        busy=true
        status=label
        lifecycleScope.launch {
            try { work() }
            catch (e: kotlinx.coroutines.CancellationException) { throw e }
            catch (e: Exception) {
                status = when (e) {
                    is java.net.SocketTimeoutException, is java.net.ConnectException -> "連不到分析電腦，請確認同一個 Wi-Fi、網址與手機展示模式。"
                    is ProjectError -> e.message.orEmpty()
                    else -> "無法完成操作，請確認連線、登入狀態與案件附件後重試。"
                }
            } finally { busy=false }
        }
    }
    private fun sessionToken() = server.sessionToken()

    private fun openNative(id: String?) {
        val token = sessionToken() ?: run { status="請先登入再開啟案件。"; return }
        task(if (id == null) "正在開啟手機檢舉人設定" else "正在準備案件與佐證檔") {
            var imported: ImportedCase? = null
            try {
                val (key, profile) = withContext(Dispatchers.IO) {
                    val email = server.accountEmail(token)
                    val account = server.accountProfile(token,email)
                    if (id != null) imported = server.importCase(this@ProjectActivity,id,token)
                    server.profileKey(email) to account
                }
                if (isFinishing || token != sessionToken()) throw ProjectError("登入已變更，請重新選擇案件。")
                val target = Intent(this@ProjectActivity,CaseActivity::class.java).putExtra("profileKey",key)
                    .putExtra("address",server.address)
                    .putExtra("profile", Bundle().apply { profile.forEach { (k,v)->putString(k,v) } })
                imported?.let { c ->
                    target.putExtra("fields", Bundle().apply { c.fields.forEach { (k,v)->putString(k,v) } })
                    target.putExtra("city",c.city).putExtra("directory",c.directory.name)
                    target.putStringArrayListExtra("uris",ArrayList(c.media.map { it.uri.toString() }))
                }
                startActivity(target)
                imported=null // CaseActivity owns the temporary directory from here.
                status=""
            } finally { imported?.directory?.deleteRecursively() }
        }
    }
    private fun save(uri: Uri) {
        val path = uri.encodedPath.orEmpty()
        if (downloadPath != null || !server.owns(uri) || !server.exportable(path)) return
        downloadPath=path
        val suggested = uri.getQueryParameter("dl") ?: if (path.endsWith("/zip")) path.split('/')[3] + ".zip" else uri.lastPathSegment
        saveDocument.launch(suggested.orEmpty().substringAfterLast('/').take(150))
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun makeWebView() = WebView(this).apply {
        // AndroidView 預設給 WRAP_CONTENT，WebView 此時會把 vh 等視窗單位算成 0（彈出視窗被壓扁）。
        layoutParams = android.view.ViewGroup.LayoutParams(android.view.ViewGroup.LayoutParams.MATCH_PARENT, android.view.ViewGroup.LayoutParams.MATCH_PARENT)
        settings.javaScriptEnabled=true
        settings.domStorageEnabled=true
        settings.allowFileAccess=false
        settings.allowContentAccess=true // Android document picker grants only the selected content URI.
        settings.cacheMode=WebSettings.LOAD_NO_CACHE
        CookieManager.getInstance().setAcceptThirdPartyCookies(this,false)
        importantForAutofill=android.view.View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
        webChromeClient=object:WebChromeClient() {
            override fun onConsoleMessage(message: ConsoleMessage?)=true
            override fun onShowFileChooser(v: WebView, cb: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                if (!server.owns(v.url?.let(Uri::parse)) || selected != null) { cb.onReceiveValue(null); return true }
                selected=cb
                val types=params.acceptTypes.filter { '/' in it }
                picker.launch(if (types.isEmpty()) arrayOf("*/*") else types.toTypedArray())
                return true
            }
        }
        setDownloadListener { url,_,_,_,_->save(Uri.parse(url)) }
        webViewClient=object:WebViewClient() {
            override fun shouldOverrideUrlLoading(v:WebView, r:WebResourceRequest): Boolean {
                val u=r.url
                if (!r.isForMainFrame) return true
                if (u.scheme=="autoform" && r.hasGesture() && server.owns(v.url?.let(Uri::parse))) {
                    if (u.host=="profile") openNative(null)
                    else if (u.host=="case") u.pathSegments.singleOrNull()?.takeIf { it.matches(Regex("[A-Za-z0-9_-]{1,100}")) }?.let(::openNative)
                    return true
                }
                if (server.owns(u) && server.exportable(u.encodedPath.orEmpty())) { if (r.hasGesture()) save(u); return true }
                return !(server.owns(u) && u.path in listOf("/","/index.html"))
            }
            override fun shouldInterceptRequest(v:WebView, r:WebResourceRequest): WebResourceResponse? {
                val u=r.url
                if (!server.owns(u)) {
                    if (u.scheme == "https" && !r.isForMainFrame && u.host in setOf("tile.openstreetmap.org","server.arcgisonline.com")) return null
                    return denied()
                }
                val path=u.path.orEmpty()
                if (path=="/api/profile" || path.startsWith("/api/autofill") ||
                    path.startsWith("/api/cases/") && "/autofill" in path) return denied()
                val asset=when(path) {
                    "/", "/index.html" -> "index.html"
                    "/static/style.css" -> "style.css"
                    "/static/app.js" -> "app.js"
                    else -> path.removePrefix("/static/").takeIf { it.matches(Regex("leaflet/(leaflet\\.(js|css)|images/[a-z0-9-]+\\.png)")) }
                }
                if (asset != null && r.method=="GET") {
                    val mime=when(asset.substringAfterLast('.')) { "html"->"text/html"; "css"->"text/css"; "png"->"image/png"; else->"application/javascript" }
                    val stream=if (asset=="index.html") assetText("project/index.html")
                        .replace("<head>","<head><script>window.autoFormMobile=true</script><style>#btn-locate{display:none}</style>")
                        .replace(Regex("<link[^>]*https://fonts\\.googleapis\\.com[^>]*>"), "")
                        .replace("https://unpkg.com/leaflet@1.9.4/dist/","/static/leaflet/")
                        .replace("電腦</button>","手機</button>").byteInputStream()
                        else assets.open("project/$asset")
                    return WebResourceResponse(mime,"utf-8",stream)
                }
                return if (!r.isForMainFrame && (path.startsWith("/api/") || path.startsWith("/files/"))) null else denied()
            }
        }
    }
    private fun denied() = WebResourceResponse("text/plain","utf-8",403,"Forbidden",emptyMap(),null)
    override fun onDestroy() {
        selected?.onReceiveValue(null)
        web.stopLoading()
        (web.parent as? android.view.ViewGroup)?.removeView(web)
        web.destroy()
        if (WebViewFeature.isFeatureSupported(WebViewFeature.PROXY_OVERRIDE))
            ProxyController.getInstance().clearProxyOverride(mainExecutorCompat()) {}
        super.onDestroy()
    }
}
