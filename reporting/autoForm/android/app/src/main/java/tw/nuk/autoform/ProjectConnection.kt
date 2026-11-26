package tw.nuk.autoform

import android.content.Context
import android.net.Uri
import androidx.core.content.FileProvider
import org.json.JSONObject
import org.json.JSONArray
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Proxy
import java.net.URI
import java.net.URL
import java.security.MessageDigest
import java.util.UUID

/** A separate web origin per analysis computer keeps cookies and drafts isolated. */
class ProjectConnection private constructor(val host: String, val port: Int) {
    val address = "http://$host:$port"
    val webHost = "p$port-" + host.replace('.', '-') + ".analysis.autoform.invalid"
    val origin = "http://$webHost"
    companion object {
        fun parse(value: String): ProjectConnection {
            val url = URI(value.trim().let { if ("://" in it) it else "http://$it" })
            require(url.scheme == "http" && url.rawPath.orEmpty() in listOf("", "/") &&
                url.userInfo == null && url.query == null && url.fragment == null)
            val parts = url.host.orEmpty().split(".")
            require(parts.size == 4)
            val ip = parts.map { part -> part.toIntOrNull()?.takeIf { it in 0..255 && it.toString() == part } ?: error("IP 格式錯誤") }
            require(ip == listOf(127,0,0,1) || ip[0] == 10 || ip[0] == 192 && ip[1] == 168 || ip[0] == 172 && ip[1] in 16..31)
            return ProjectConnection(parts.joinToString("."), (if (url.port == -1) 8765 else url.port).also { require(it in 1..65535) })
        }
    }
    fun owns(uri: Uri?) = uri?.scheme == "http" && uri.encodedAuthority == webHost
    /** 專案網頁登入後寫在本 App WebView 的 tr_token cookie。 */
    fun sessionToken() = android.webkit.CookieManager.getInstance().getCookie(origin)?.split(';')
        ?.map { it.trim() }?.firstOrNull { it.startsWith("tr_token=") }?.substringAfter('=')
        ?.takeIf { it.matches(Regex("[A-Za-z0-9_-]{1,256}")) }
    private fun <T> read(path: String, token: String?, body: JSONObject? = null, action: (InputStream, Long) -> T): T {
        require(token == null || token.matches(Regex("[A-Za-z0-9_-]{1,256}")))
        val proxy = Proxy(Proxy.Type.HTTP, InetSocketAddress(host, port))
        val http = URL(origin + path).openConnection(proxy) as HttpURLConnection
        try {
            http.instanceFollowRedirects = false
            http.connectTimeout = 10000
            http.readTimeout = 45000
            http.useCaches = false
            if (token != null) http.setRequestProperty("X-Auth-Token", token)
            http.setRequestProperty("Accept-Encoding", "identity")
            if (body != null) {
                val bytes = body.toString().toByteArray(Charsets.UTF_8)
                http.requestMethod = "POST"
                http.doOutput = true
                http.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                http.setFixedLengthStreamingMode(bytes.size)
                http.outputStream.use { it.write(bytes) }
            }
            when (http.responseCode) {
                200 -> return http.inputStream.use { action(it, http.contentLengthLong) }
                400 -> throw ProjectError(runCatching {
                    JSONObject(http.errorStream.bufferedReader(Charsets.UTF_8).readText()).getString("error")
                }.getOrNull() ?: "資料格式不正確。")
                401, 403 -> throw ProjectError("登入已失效，請重新登入。")
                404 -> throw ProjectError("案件或佐證檔已不存在，請回到案件紀錄確認。")
                else -> throw ProjectError("分析主機未能提供案件資料。")
            }
        } finally { http.disconnect() }
    }
    private fun json(path: String, token: String) = read(path,token) { input,_ ->
        JSONObject(input.bufferedReader(Charsets.UTF_8).readText())
    }
    fun checkService() = read("/api/cities",null) { input,_ ->
        val cities = JSONArray(input.bufferedReader(Charsets.UTF_8).readText())
        if ((0 until cities.length()).none { cities.optJSONObject(it)?.optString("code") == "kaohsiung" })
            throw ProjectError("此網址並非專案介面，請輸入手機展示模式顯示的網址。")
    }
    fun exportable(path: String) = path.matches(Regex("/api/cases/[A-Za-z0-9_-]+/zip|/files/cases/[A-Za-z0-9_-]+/(info\\.txt|case\\.json|evidence\\.(mp4|png|jpe?g))"))
    fun export(path: String, token: String, output: java.io.OutputStream) {
        require(exportable(path))
        read(path,token) { input,size ->
            val count = input.copyTo(output)
            if (size >= 0 && count != size) throw ProjectError("下載中斷，請重新儲存。")
        }
    }
    fun accountEmail(token: String): String =
        json("/api/auth/me",token).getJSONObject("user").getString("email").also { require(it.isNotBlank()) }
    fun profileKey(email: String): String {
        val hash = MessageDigest.getInstance("SHA-256").digest((address + "\n" + email.lowercase()).toByteArray())
        return "project_" + hash.joinToString("") { "%02x".format(it) }
    }
    /** 分析網頁註冊帳號時填的檢舉人資料；Email 沒填時用登入帳號的 Email。 */
    fun accountProfile(token: String, email: String): Map<String,String> {
        val data = json("/api/profile",token)
        return profileFields.keys.associateWith { data.optString(it).trim() }
            .let { if (it["reporter_email"].isNullOrBlank()) it + ("reporter_email" to email) else it }
    }
    /** 手機上補填的資料存回帳號，網頁與下次開案件都會用到；格式錯誤時拋出伺服器的說明。 */
    fun saveAccountProfile(token: String, values: Map<String,String>): Map<String,String> {
        val body = JSONObject().apply { profileFields.keys.forEach { put(it, values[it].orEmpty().trim()) } }
        val saved = read("/api/profile",token,body) { input,_ -> JSONObject(input.bufferedReader(Charsets.UTF_8).readText()) }
        return profileFields.keys.associateWith { saved.optString(it) }
    }
    fun importCase(context: Context, id: String, token: String): ImportedCase {
        require(id.matches(Regex("[A-Za-z0-9_-]{1,100}")))
        val data = json("/api/cases/$id",token)
        require(data.getString("id") == id && data.optString("status") != "submitted") { "此案件已送出或無法開啟。" }
        val city = data.getString("city").replace('臺','台')
        require(city in Sites(context).drivers) { "此縣市尚無官網填表流程。" }
        val source = data.getJSONObject("fields")
        val fields = mapOf("violation_date" to "date","violation_time" to "time","district" to "district",
            "violation_location" to "location","license_plate" to "plate","car_type" to "vehicle_type",
            "violation_category" to "category","violation_subcategory" to "fact","violation_description" to "description")
            .mapValues { (_, key) -> source.optString(key) }
        val dir = File(File(context.cacheDir,"project-evidence"), UUID.randomUUID().toString())
        check(dir.mkdirs())
        try {
            val evidence = data.getJSONArray("files").strings().distinct().filter { it.matches(Regex("evidence\\.(mp4|jpg|jpeg|png)",RegexOption.IGNORE_CASE)) }
            val event = data.optJSONObject("violation")
            for (key in listOf("clip","snapshot")) {
                val original = event?.optString(key).orEmpty()
                if (original.isNotBlank() && original != "null") {
                    require(evidence.any { it.substringAfterLast('.').equals(original.substringAfterLast('.'),true) }) { "案件缺少分析結果列出的佐證檔。" }
                }
            }
            val media = evidence.map { name ->
                val file = File(dir,name)
                read("/files/cases/$id/$name",token) { stream, size ->
                    val copied = file.outputStream().use { stream.copyTo(it) }
                    if (copied == 0L || size >= 0 && size != copied) throw IOException("佐證檔下載不完整，請重試。")
                }
                Media(FileProvider.getUriForFile(context,context.packageName + ".evidence",file),name,name.endsWith(".mp4",true))
            }
            return ImportedCase(city,fields,media,dir)
        } catch (e: Exception) { dir.deleteRecursively(); throw e }
    }
}
data class ImportedCase(val city: String, val fields: Map<String,String>, val media: List<Media>, val directory: File)
class ProjectError(message: String): IOException(message)
