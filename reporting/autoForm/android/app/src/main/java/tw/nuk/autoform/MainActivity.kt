package tw.nuk.autoform

import android.graphics.Bitmap
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import androidx.documentfile.provider.DocumentFile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent { MaterialTheme { Surface { ProjectHome() } } }
    }
}

@Composable
fun AutoFormApp(profileKey: String = "reporter", projectCity: String? = null,
                initialFields: Map<String,String> = emptyMap(), initialMedia: List<Media> = emptyList(),
                onExit: (() -> Unit)? = null, accountProfile: Map<String,String>? = null,
                saveAccountProfile: (suspend (Map<String,String>) -> Map<String,String>)? = null) {
    val context = LocalContext.current
    val sites = remember { Sites(context) }
    val prefs = remember(profileKey) { runCatching { profileStore(context,profileKey) }.getOrNull() }
    // 有分析網頁帳號時以帳號資料為準；帳號缺的欄位才用本機先前存過的值補上。
    val profile = remember { mutableStateMapOf<String, String>().apply {
        profileFields.keys.forEach { key ->
            put(key, accountProfile?.get(key)?.takeIf(String::isNotBlank) ?: prefs?.getString(key, "").orEmpty())
        }
    } }
    val fields = remember { mutableStateMapOf<String, String>().apply { putAll(initialFields) } }
    var media by remember { mutableStateOf(initialMedia) }
    var page by remember { mutableIntStateOf(if (projectCity != null && profile.values.all { it.isNotBlank() }) 3 else 0) }
    var city by remember { mutableStateOf(projectCity ?: "高雄市") }
    var message by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val folder = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri ->
        if (uri != null) scope.launch {
            busy = true
            try {
                val data = withContext(Dispatchers.IO) { readCase(context, uri) }
                fields.clear()
                fields.putAll(data.fields)
                media = data.media
                message = "已載入案件與 ${media.size} 個附件"
            } catch (_: Exception) { message = "載入失敗，請選擇含 UTF-8 info.txt 的資料夾。" }
            finally { busy = false }
        }
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.PickMultipleVisualMedia()) { uris ->
        if (uris.isNotEmpty()) scope.launch {
            busy = true
            try {
                val added = withContext(Dispatchers.IO) {
                    uris.filter { it.scheme == "content" }.map { uri ->
                        Media(uri, DocumentFile.fromSingleUri(context, uri)?.name ?: "附件",
                            context.contentResolver.getType(uri)?.startsWith("video/") == true)
                    }
                }
                media = (media + added).distinctBy { it.uri }
            } catch (_: Exception) { message = "無法開啟選取的附件" }
            finally { busy = false }
        }
    }

    BackHandler(page > 0 && !busy) { if (onExit != null) onExit() else page--; message = "" }
    Column(Modifier.fillMaxSize().safeDrawingPadding().imePadding()) {
        if (page == 3) {
            FormScreen(city, sites, profile.toMap() + fields.toMap(), media)
            return@Column
        }
        Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(listOf("個資設定", "案件", "縣市選擇")[page], style = MaterialTheme.typography.headlineMedium)
            when (page) {
                0 -> {
                    profileFields.forEach { (key, label) ->
                        OutlinedTextField(profile[key].orEmpty(), { profile[key] = it },
                            Modifier.fillMaxWidth(), label = { Text(label) }, singleLine = true,
                            visualTransformation = if (key == "reporter_id") PasswordVisualTransformation() else VisualTransformation.None)
                    }
                    if (saveAccountProfile != null) Text("與分析網頁的帳號同步：請補齊空白欄位，儲存後網頁與之後的案件都會自動帶入。")
                    else if (prefs == null) Text("無法開啟加密儲存，請確認裝置的金鑰庫可用。")
                    Button(enabled = !busy && (prefs != null || saveAccountProfile != null), onClick = {
                        val missing = profileFields.filterKeys { profile[it].isNullOrBlank() }.values
                        if (projectCity != null && missing.isNotEmpty()) { message = "官網填表需要：" + missing.joinToString("、"); return@Button }
                        scope.launch {
                            busy = true
                            try {
                                val values = profile.mapValues { it.value.trim() }
                                val saved = saveAccountProfile?.invoke(values) ?: values
                                profile.putAll(saved)
                                prefs?.edit()?.apply { saved.forEach { (k, v) -> putString(k, v) } }?.commit()
                                if (prefs == null && saveAccountProfile == null) { message = "個資儲存失敗"; return@launch }
                                if (onExit != null && projectCity == null) onExit()
                                else page = if (projectCity != null) 3 else 1
                                message = ""
                            } catch (e: ProjectError) { message = e.message.orEmpty() }
                            catch (e: java.io.IOException) { message = "連不到分析電腦，個資尚未儲存。" }
                            finally { busy = false }
                        }
                    }) { Text(if (onExit == null) "儲存並前往案件" else if (projectCity == null) "儲存並返回" else "儲存並開啟官網填表") }
                }
                1 -> {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(enabled = !busy, onClick = { folder.launch(null) }) { Text("載入資料夾") }
                        OutlinedButton(enabled = !busy, onClick = {
                            picker.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageAndVideo))
                        }) { Text("從相簿加入") }
                    }
                    caseFields.forEach { (key, label) ->
                        OutlinedTextField(fields[key].orEmpty(), { fields[key] = it }, Modifier.fillMaxWidth(),
                            enabled = !busy, label = { Text(label) }, singleLine = key != "violation_description")
                        if (key == "violation_subcategory") ChoiceMenu("選擇違規事實", sites.facts) { fields[key] = it }
                    }
                    MediaStrip(media) { item -> media = media - item }
                    Button(enabled = !busy, onClick = {
                        city = recommendCity(fields["violation_location"].orEmpty() + fields["district"].orEmpty()) ?: city
                        page = 2
                        message = ""
                    }) { Text("選擇縣市") }
                }
                2 -> {
                    ChoiceMenu(city, sites.urls.keys.toList()) { city = it }
                    if (city in sites.drivers) {
                        val chosen = remember(city, media) { caseAttachments(sites.drivers.getValue(city), sites.cityConfig(city), media) }
                        if (chosen.size < media.size) Text("依${city}附件限制，本次帶入 ${chosen.size} / ${media.size} 件。")
                    } else Text("此縣市使用電子郵件受理。")
                    Button(enabled = city in sites.drivers, onClick = { page = 3 }) { Text("開啟${city}填表頁") }
                    TextButton(onClick = { page = 1 }) { Text("返回案件") }
                }
            }
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (message.isNotEmpty()) Text(message)
        }
    }
}

@Composable
fun ChoiceMenu(label: String, items: List<String>, choose: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Box {
        OutlinedButton(onClick = { open = true }) { Text(label + " ▾") }
        DropdownMenu(open, { open = false }, Modifier.heightIn(max = 360.dp)) {
            items.forEach { item -> DropdownMenuItem(text = { Text(item) }, onClick = { choose(item); open = false }) }
        }
    }
}

@Composable
private fun MediaStrip(media: List<Media>, remove: (Media) -> Unit) {
    val context = LocalContext.current
    Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        media.forEach { item -> key(item.uri) {
            val bitmap by produceState<Bitmap?>(null, item.uri) {
                value = withContext(Dispatchers.IO) { runCatching { thumbnail(context, item) }.getOrNull() }
            }
            Column(Modifier.width(112.dp)) {
                bitmap?.let { Image(it.asImageBitmap(), item.name, Modifier.size(96.dp)) }
                Text(item.name, maxLines = 2)
                TextButton(onClick = { remove(item) }) { Text("移除") }
            }
        } }
    }
}
