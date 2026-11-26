package tw.nuk.autoform

import java.text.Normalizer
import java.util.Locale
import kotlin.math.round
import org.json.JSONArray
import org.json.JSONObject

data class Option(val value: String, val text: String = value)
data class Ranked(val option: Option, val score: Double)
data class Match(val option: Option, val score: Double, val candidates: List<Ranked>, val ambiguous: Boolean)

private fun nfkc(s: String) = Normalizer.normalize(s, Normalizer.Form.NFKC)
private fun basic(s: String) = nfkc(s).replace('臺', '台').replace('颱', '台')
    .lowercase(Locale.ROOT).filter { it.isLetterOrDigit() }
private val article = Regex("""第\s*(\d{1,3})\s*條(?:\s*之\s*(\d{1,2}))?(?:\s*第\s*(\d{1,2})\s*項)?(?:\s*第\s*(\d{1,2})\s*款)?""")
private val dotted = Regex("""(?<!\d)(\d{1,3})[.-](\d{1,2})(?:[.-](\d{1,2}))?(?!\d)""")
private val parens = Regex("""[(（\[【〔][^)）\]】〕]*[)）\]】〕]""")
private val trailing = Regex("""(?:[。.]?\((?:第?\d+(?:\.\d+)*條?(?:之\d+)?[、,]?)+\))+$""")
private val placeholder = Regex("^(請選擇|請選取|請先選擇|選擇|--+|－+|==+|全部|不拘|none|select)", RegexOption.IGNORE_CASE)
private val cities = "台北市 新北市 基隆市 桃園市 新竹市 新竹縣 苗栗縣 台中市 彰化縣 南投縣 雲林縣 嘉義市 嘉義縣 台南市 高雄市 屏東縣 宜蘭縣 花蓮縣 台東縣 澎湖縣 金門縣 連江縣".split(" ")
private fun rounded(n: Double) = round(n * 10000) / 10000
fun JSONArray.strings() = List(length()) { getString(it) }

class OptionMatcher(settings: JSONObject) {
    private val aliases = buildMap {
        val groups = settings.getJSONObject("synonyms")
        for (name in groups.keys()) for (alias in groups.getJSONArray(name).strings()) {
            if (basic(alias).isNotEmpty() && basic(alias) != basic(name)) put(basic(alias), basic(name))
        }
    }
    private val aliasPattern = aliases.keys.sortedByDescending(String::length)
        .takeIf { it.isNotEmpty() }?.joinToString("|", transform = Regex::escape)?.toRegex()
    private val discriminators = settings.getJSONArray("discriminators").strings().map(::basic)

    fun normalize(s: String): String = basic(s).let { clean ->
        aliasPattern?.replace(clean) { aliases.getValue(it.value) } ?: clean
    }

    fun lawRefs(s: String): List<List<String>> {
        val text = nfkc(s)
        val articles = article.findAll(text).map { match ->
            val (number, sub, item, clause) = match.destructured
            listOf(number + if (sub.isEmpty()) "" else "-$sub", item, clause).filter(String::isNotEmpty)
        }
        val codes = dotted.findAll(text).map { it.groupValues.drop(1).filter(String::isNotEmpty) }
        return (articles + codes).toList()
    }

    fun options(values: List<Option>) = values.filter { it.text.isNotBlank() && !placeholder.containsMatchIn(it.text.trim()) }

    // SequenceMatcher's longest contiguous block, then its left and right matching blocks.
    private fun blocks(a: String, b: String): Pair<Int, Int> {
        var longest = 0
        fun count(a0: Int, a1: Int, b0: Int, b1: Int): Int {
            if (a0 >= a1 || b0 >= b1) return 0
            val run = IntArray(b1 - b0 + 1)
            var size = 0
            var ai = 0
            var bi = 0
            for (i in a0 until a1) {
                var previous = 0
                for (j in b0 until b1) {
                    val k = j - b0 + 1
                    val priorRow = run[k]
                    run[k] = if (a[i] == b[j]) previous + 1 else 0
                    previous = priorRow
                    if (run[k] > size) { size = run[k]; ai = i + 1 - size; bi = j + 1 - size }
                }
            }
            if (size == 0) return 0
            longest = maxOf(longest, size)
            return size + count(a0, ai, b0, bi) + count(ai + size, a1, bi + size, b1)
        }
        return count(0, a.length, 0, b.length) to longest
    }

    private fun similarity(q: String, o: String): Double {
        if (q.isEmpty() || o.isEmpty()) return 0.0
        if (q == o) return 1.0
        var score = when {
            q in o -> 0.80 + 0.15 * q.length / o.length
            o.length >= 2 && o in q -> 0.70 + 0.20 * o.length / q.length
            else -> 0.0
        }
        fun grams(s: String) = if (s.length < 2) setOf(s) else s.windowed(2).toSet()
        val a = grams(q)
        val b = grams(o)
        score = maxOf(score, 0.95 * 5 * a.intersect(b).size / (4 * a.size + b.size))
        if (q.length <= 10) {
            val counts = o.groupingBy { it }.eachCount()
            val common = q.groupingBy { it }.eachCount().entries.sumOf { minOf(it.value, counts[it.key] ?: 0) }
            score = maxOf(score, 0.8 * 5 * common / (4 * q.length + o.length))
        }
        val (total, longest) = blocks(q, o)
        score = maxOf(score, 1.8 * total / (q.length + o.length))
        if (longest >= 3) score = maxOf(score, 0.45 + 0.25 * longest / minOf(q.length, o.length) +
            0.15 * longest / maxOf(q.length, o.length))
        return score
    }

    private fun variants(s: String): List<String> {
        fun strip(t: String) = normalize(dotted.replace(article.replace(t, ""), ""))
        val text = nfkc(s)
        return listOf(strip(text), strip(parens.replace(text, ""))).distinct()
    }

    fun scoreText(query: String, option: String): Double {
        val qv = variants(query)
        val ov = variants(option)
        if ((qv + ov).all(String::isEmpty) || qv.all(String::isEmpty) || ov.all(String::isEmpty)) return 0.0
        val best = qv.maxOf { q -> ov.maxOf { o ->
            val score = similarity(q, o)
            val different = if (score < 0.4) 0 else discriminators.count { q.split(it).size != o.split(it).size }
            score - minOf(0.36, different * 0.12)
        } }
        val depths = lawRefs(query).flatMap { q -> lawRefs(option).map { o ->
            q.zip(o).takeWhile { it.first == it.second }.size
        } }
        val adjustment = when (val depth = depths.maxOrNull()) {
            null -> 0.0
            0 -> -0.2
            else -> minOf(0.15, 0.05 * depth)
        }
        return (best.coerceAtLeast(0.0) + adjustment).coerceIn(0.0, 1.0)
    }

    fun rankOptions(queries: List<String>, values: List<Option>): List<Ranked> {
        val weights = listOf(1.0, 0.95, 0.9, 0.85)
        return rankWeighted(queries.mapIndexed { i, q -> q to weights[minOf(i, weights.lastIndex)] }, values)
    }

    fun rankWeighted(queries: List<Pair<String, Double>>, values: List<Option>): List<Ranked> {
        val usable = queries.filter { it.first.isNotBlank() }
        if (usable.isEmpty()) return emptyList()
        return options(values).map { opt ->
            val best = usable.maxOf { (q, weight) ->
                weight * if (opt.value.isNotEmpty() && normalize(opt.value) == normalize(q)) 1.0 else scoreText(q, opt.text)
            }
            Ranked(opt, rounded(best))
        }.sortedByDescending { it.score }
    }

    fun bestMatch(queries: List<String>, values: List<Option>, threshold: Double = 0.55): Match? {
        val ranked = rankOptions(queries, values)
        val top = ranked.firstOrNull()?.takeIf { it.score >= threshold } ?: return null
        val second = ranked.getOrNull(1)
        val ambiguous = second != null && normalize(second.option.text) != normalize(top.option.text) && top.score - second.score < 0.03
        return Match(top.option, top.score, ranked, ambiguous)
    }

    fun explainNoMatch(queries: List<String>, values: List<Option>) =
        "比對不到符合的選項：" + rankOptions(queries, values).take(5).joinToString { it.option.text }

    fun exactMatch(text: String, values: List<Option>): Match? {
        fun key(s: String) = nfkc(s).filterNot(Char::isWhitespace)
        fun core(s: String) = trailing.replace(s, "").trimEnd('。', '.')
        val target = key(text)
        if (target.isEmpty()) return null
        val opts = options(values)
        val hits = opts.filter { key(it.text) == target }.ifEmpty {
            val wanted = core(target)
            listOfNotNull(opts.singleOrNull { wanted.isNotEmpty() && core(key(it.text)) == wanted })
        }
        return hits.firstOrNull()?.let { Match(it, 1.0, hits.map { o -> Ranked(o, 1.0) }, hits.size > 1) }
    }

    fun normalizeDistrict(s: String, city: String = ""): String {
        var name = basic(s)
        val prefix = (listOf(basic(city)) + cities).firstOrNull { it.isNotEmpty() && name.startsWith(it) && name.length > it.length }
        if (prefix != null) name = name.removePrefix(prefix)
        name = name.replace(Regex("警察局|分局|派出所|分駐所"), "")
        return if (name.length >= 2 && name.last() in "區鄉鎮市") name.dropLast(1) else name
    }

    fun matchDistrict(name: String, values: List<Option>, city: String = "", threshold: Double = 0.75): Match? {
        val target = normalizeDistrict(name, city)
        if (target.isEmpty()) return null
        val opts = options(values)
        val hits = opts.filter { normalizeDistrict(it.text, city) == target || normalizeDistrict(it.value, city) == target }
        if (hits.isNotEmpty()) return Match(hits.first(), 1.0, hits.map { Ranked(it, 1.0) } +
            (opts - hits.toSet()).map { Ranked(it, 0.0) }, hits.size > 1)
        val ranked = opts.map {
            val base = normalizeDistrict(it.text, city)
            val score = when {
                base.length >= 2 && target.startsWith(base) -> 0.9
                minOf(base.length, target.length) < 2 -> 0.0
                else -> 2.0 * blocks(target, base).first / (target.length + base.length)
            }
            Ranked(it, rounded(score))
        }.sortedByDescending { it.score }
        return ranked.firstOrNull()?.takeIf { it.score >= threshold }?.let { Match(it.option, it.score, ranked, false) }
    }
}
