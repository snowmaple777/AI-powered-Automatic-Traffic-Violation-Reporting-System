package tw.nuk.autoform

import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp

@Composable
fun ProjectHome() {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("analysis-host",0) }
    var address by rememberSaveable { mutableStateOf(prefs.getString("address","").orEmpty()) }
    var manual by rememberSaveable { mutableStateOf(false) }
    var error by rememberSaveable { mutableStateOf("") }
    var pendingAddress by rememberSaveable { mutableStateOf<String?>(null) }
    val focus = LocalFocusManager.current
    fun openProject(endpoint: String) {
        prefs.edit().putString("address", endpoint).apply()
        context.startActivity(Intent(context, ProjectActivity::class.java).putExtra("address", endpoint))
    }
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { allowed ->
        val endpoint = pendingAddress
        pendingAddress = null
        if (allowed && endpoint != null) openProject(endpoint)
        else if (!allowed) error = "請在系統設定允許本 App 存取區域網路，再重新連線。"
    }
    fun submitAddress() {
        focus.clearFocus()
        val endpoint = runCatching { ProjectConnection.parse(address).address }.getOrNull()
        if (endpoint == null) {
            error = if (address.isBlank()) "請先輸入「手機展示模式」視窗中「手機（同一個 Wi-Fi）」那一行的網址。"
                    else "網址格式有誤。請輸入手機展示模式的 http:// 網址，或直接輸入電腦的區網 IPv4 位址。"
            return
        }
        error = ""
        val lan = "android.permission.ACCESS_LOCAL_NETWORK"
        if (Build.VERSION.SDK_INT >= 37 && context.checkSelfPermission(lan) != PackageManager.PERMISSION_GRANTED) {
            pendingAddress = endpoint
            permission.launch(lan)
        } else openProject(endpoint)
    }
    if (manual) {
        BackHandler { manual = false }
        AutoFormApp()
        return
    }
    Column(Modifier.fillMaxSize().safeDrawingPadding().imePadding().verticalScroll(rememberScrollState()).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("交通違規檢舉",style=MaterialTheme.typography.headlineMedium)
        Text("手機與分析電腦連上同一個 Wi-Fi，輸入電腦「手機展示模式」顯示的網址。")
        OutlinedTextField(
            value = address,
            onValueChange = { address = it; error = "" },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            label = { Text("分析電腦網址") },
            isError = error.isNotEmpty(),
            supportingText = { Text(error.ifEmpty { "範例：http://192.168.1.10:8765" }) },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Go),
            keyboardActions = KeyboardActions(onGo = { submitAddress() })
        )
        Button(onClick = ::submitAddress) { Text("進入影片分析與檢舉") }
        OutlinedButton(onClick={manual=true}) { Text("手動填表或載入已有案件") }
    }
}
