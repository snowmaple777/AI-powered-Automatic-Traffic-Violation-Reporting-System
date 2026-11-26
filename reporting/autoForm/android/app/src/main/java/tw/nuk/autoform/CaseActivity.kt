package tw.nuk.autoform

import android.os.Bundle
import android.net.Uri
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.material3.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

class CaseActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val profileKey = intent.getStringExtra("profileKey") ?: return finish()
        val city = intent.getStringExtra("city")
        val fields = intent.getBundleExtra("fields")?.let { b -> caseFields.keys.associateWith { b.getString(it).orEmpty() } }.orEmpty()
        val media = intent.getStringArrayListExtra("uris").orEmpty().map { value ->
            val uri = Uri.parse(value)
            val name = requireNotNull(uri.lastPathSegment)
            Media(uri,name,name.endsWith(".mp4",true))
        }
        // 分析網頁帳號的檢舉人資料：有就以帳號為準，手機補填的內容存回帳號。
        val account = intent.getBundleExtra("profile")?.let { b -> profileFields.keys.associateWith { b.getString(it).orEmpty() } }
        val server = intent.getStringExtra("address")?.let { runCatching { ProjectConnection.parse(it) }.getOrNull() }
        val save: (suspend (Map<String,String>) -> Map<String,String>)? = server?.let { s -> { values ->
            withContext(Dispatchers.IO) {
                val token = s.sessionToken() ?: throw ProjectError("登入已失效，請回到分析網頁重新登入。")
                s.saveAccountProfile(token, values)
            }
        } }
        setContent { MaterialTheme { Surface {
            AutoFormApp(profileKey,city,fields,media, onExit = ::finish, accountProfile = account, saveAccountProfile = save)
        } } }
    }
    override fun onDestroy() {
        super.onDestroy()
        if (isFinishing) intent.getStringExtra("directory")?.takeIf { it.matches(Regex("[0-9a-f-]{36}")) }?.let {
            File(File(cacheDir,"project-evidence"),it).deleteRecursively()
        }
    }
}
