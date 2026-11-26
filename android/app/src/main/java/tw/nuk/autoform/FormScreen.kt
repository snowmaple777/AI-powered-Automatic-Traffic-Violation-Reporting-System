package tw.nuk.autoform

import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

@Composable
fun FormScreen(city: String, sites: Sites, data: Map<String, String>, media: List<Media>) {
    val context = LocalContext.current
    val browser = remember(city) { FormBrowser(context, sites, data, media, city) }
    DisposableEffect(browser) { onDispose { browser.close() } }
    var logOpen by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize()) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 4.dp)) {
            Text(browser.step.label, style = MaterialTheme.typography.titleMedium)
            Text(browser.status)
            if (browser.unmatched.isNotEmpty()) Text("請手動核對：" + browser.unmatched.joinToString("、"),
                color = MaterialTheme.colorScheme.error)
            Row(Modifier.horizontalScroll(rememberScrollState())) {
                if (browser.mayRetryUpload) TextButton(onClick = browser::retryUpload) { Text("確認未上傳，重試此檔") }
                TextButton(onClick = browser::retry, enabled = browser.step !in setOf(Step.Pending, Step.Done)) { Text("重新注入本步驟") }
                TextButton(onClick = { logOpen = true }) { Text("步驟紀錄") }
            }
        }
        AndroidView(factory = { browser.view }, modifier = Modifier.fillMaxSize())
    }
    if (logOpen) AlertDialog(onDismissRequest = { logOpen = false }, title = { Text("步驟紀錄") },
        text = { Text(browser.logs.joinToString("\n"), Modifier.verticalScroll(rememberScrollState())) },
        confirmButton = { TextButton(onClick = { logOpen = false }) { Text("關閉") } })
}
