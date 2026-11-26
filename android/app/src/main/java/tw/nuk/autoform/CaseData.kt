package tw.nuk.autoform

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.os.Build
import android.util.Size
import androidx.documentfile.provider.DocumentFile
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import org.json.JSONObject

val profileFields = mapOf("reporter_name" to "姓名", "reporter_id" to "身分證字號",
    "reporter_phone" to "電話", "reporter_email" to "Email", "reporter_address" to "通訊地址")
val caseFields = mapOf("violation_date" to "違規日期", "violation_time" to "違規時間",
    "district" to "違規行政區", "violation_location" to "交通違規地點", "license_plate" to "車牌號碼",
    "car_type" to "違規車種類型", "violation_category" to "違規類別",
    "violation_subcategory" to "違規事實", "violation_description" to "違規事實敘述")

@Suppress("DEPRECATION")
fun profileStore(context: Context, name: String = "reporter") = EncryptedSharedPreferences.create(context, name,
    MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
    EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
    EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM)

fun parseInfo(lines: Sequence<String>): Map<String, String> {
    val raw = lines.filter { ':' in it }.associate {
        it.substringBefore(':').trim() to it.substringAfter(':').trim()
    }
    return caseFields.mapValues { (key, label) ->
        if (key == "district") listOf(label, "交通違規地點行政區", "違規行政區(必填)")
            .firstNotNullOfOrNull { raw[it]?.takeIf(String::isNotBlank) }.orEmpty()
        else raw[label].orEmpty()
    }
}

val cityRules = listOf(
    "高雄市" to listOf("高雄", "苓雅", "三民", "左營"),
    "台南市" to listOf("台南", "永康", "安平", "東區", "中西區"),
    "屏東縣" to listOf("屏東", "潮州", "恆春"),
    "雲林縣" to listOf("雲林", "斗六", "虎尾", "西螺"),
    "台中市" to listOf("台中", "臺中", "西屯", "北屯", "大里"),
    "彰化縣" to listOf("彰化", "員林", "鹿港", "和美"),
    "南投縣" to listOf("南投", "草屯", "埔里", "竹山"),
    "宜蘭縣" to listOf("宜蘭", "羅東", "礁溪", "頭城", "蘇澳"),
    "花蓮縣" to listOf("花蓮", "吉安", "壽豐", "新城", "玉里"),
    "台東縣" to listOf("台東", "臺東", "東河", "成功", "關山", "池上", "卑南", "太麻里", "綠島", "蘭嶼"),
    "新竹縣" to listOf("新竹縣", "竹北", "竹東", "關西", "新埔", "湖口", "新豐", "芎林"),
    "新竹市" to listOf("新竹市", "香山", "清大", "交大", "城隍廟", "光復路"),
    "澎湖縣" to listOf("澎湖", "馬公", "西嶼", "白沙", "湖西", "七美", "望安"),
    "基隆市" to listOf("基隆", "中正區", "信義區", "仁愛區", "七堵", "暖暖", "廟口"),
    "嘉義市" to listOf("嘉義市", "東區", "西區"),
    "嘉義縣" to listOf("嘉義", "民雄", "朴子", "太保"),
    "台北市" to listOf("台北", "信義"),
    "新北市" to listOf("新北", "板橋"),
    "苗栗縣" to listOf("苗栗", "頭份", "竹南")
)
fun recommendCity(location: String) = cityRules.firstOrNull { (_, names) ->
    names.any { it in location }
}?.first

fun Context.assetText(file: String) = assets.open(file).bufferedReader().use { it.readText() }
class Sites(private val context: Context) {
    val drivers = mapOf(
        "高雄市" to "kaohsiung", "屏東縣" to "pingtung", "台南市" to "tainan", "嘉義縣" to "chiayi",
        "雲林縣" to "yunlin", "彰化縣" to "changhua", "南投縣" to "nantou", "苗栗縣" to "miaoli",
        "宜蘭縣" to "yilan", "花蓮縣" to "hualien", "台東縣" to "taitung", "澎湖縣" to "penghu",
        "新竹市" to "hsinchu_city", "新竹縣" to "hsinchu_county", "基隆市" to "keelung", "新北市" to "new_taipei",
        "桃園市" to "taoyuan", "台中市" to "taichung", "嘉義市" to "chiayi_city", "台北市" to "taipei")
    fun cityConfig(city: String) = JSONObject(context.assetText(drivers.getValue(city) + ".json"))
    val urls = context.assetText("sites.txt").lineSequence().filter { ':' in it }
        .associate { it.substringBefore(':').trim() to it.substringAfter(':').trim() }
    val config = cityConfig("高雄市")
    val catalog = JSONObject(context.assetText("violation_catalog.json")).getJSONObject("violations")
    val facts = (config.getJSONObject("violation_map").keys().asSequence() + catalog.keys().asSequence())
        .filterNot { it.startsWith("_") }.distinct().toList()

    fun startUrl(config: JSONObject, city: String) =
        config.optString("start_url").ifBlank { urls.getValue(city) }
}

data class Media(val uri: Uri, val name: String, val video: Boolean)
fun caseAttachments(driver: String, config: JSONObject, media: List<Media>): List<Media> {
    val selectors = config.getJSONObject("selectors")
    if (driver == "chiayi_city") {
        var images = 0
        var videos = 0
        return media.filter { if (it.video) ++videos <= 2 else ++images <= 3 }
    }
    val limit = selectors.optJSONArray("file_inputs")?.length() ?: selectors.optJSONArray("file_uploads")?.length()
        ?: when (driver) {
            "kaohsiung", "taichung" -> 3
            "hsinchu_city" -> 5
            "nantou", "hsinchu_county" -> 1
            else -> media.size
        }
    return media.take(limit)
}
data class CaseFolder(val fields: Map<String, String>, val media: List<Media>)

fun readCase(context: Context, uri: Uri): CaseFolder {
    val files = requireNotNull(DocumentFile.fromTreeUri(context, uri)).listFiles().filter { it.isFile }
    val info = requireNotNull(files.find { it.name == "info.txt" })
    val fields = requireNotNull(context.contentResolver.openInputStream(info.uri))
        .bufferedReader(Charsets.UTF_8).useLines(::parseInfo)
    val media = files.mapNotNull { file ->
        val name = file.name ?: return@mapNotNull null
        val ext = name.substringAfterLast('.').lowercase()
        if (ext in setOf("jpg", "jpeg", "png", "mp4")) Media(file.uri, name, ext == "mp4") else null
    }.sortedBy { it.name }
    return CaseFolder(fields, media)
}

fun thumbnail(context: Context, media: Media): Bitmap? {
    val resolver = context.contentResolver
    if (Build.VERSION.SDK_INT >= 29) {
        runCatching { resolver.loadThumbnail(media.uri, Size(144, 144), null) }.getOrNull()?.let { return it }
    }
    if (media.video) {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(context, media.uri)
            if (Build.VERSION.SDK_INT >= 27) retriever.getScaledFrameAtTime(-1, MediaMetadataRetriever.OPTION_CLOSEST_SYNC, 144, 144)
            else retriever.frameAtTime
        } finally { retriever.release() }
    }
    val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    resolver.openInputStream(media.uri)?.use { BitmapFactory.decodeStream(it, null, opts) }
    opts.inJustDecodeBounds = false
    opts.inSampleSize = Integer.highestOneBit((maxOf(opts.outWidth, opts.outHeight) / 144).coerceAtLeast(1))
    return resolver.openInputStream(media.uri)?.use { BitmapFactory.decodeStream(it, null, opts) }
}
