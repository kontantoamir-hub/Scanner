package com.scanner.dashboard

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import kotlin.math.sqrt

// ---------------- ثوابت مطابقة لـ scanner.py ----------------
const val FEE_PCT = 0.2          // TRADING_FEE_PCT
const val BREAKEVEN_BAND = 0.1   // BREAKEVEN_BAND_PCT
const val ACTIVE_FILE = "closed_trades.json"
const val OPEN_FILE = "open_positions.json"
const val CHAIN_FILE = "archive_gists_chain.json"
const val ARCHIVE_PREFIX = "closed_trades_archive_"
const val REPORTS_FILE = "app_reports.json"

// ---------------- التحديث التلقائي ----------------
const val REFRESH_SECONDS = 15                 // الصفقات المفتوحة + الأسعار + التقارير (طلبات شرطية ETag: خفيفة)
const val ARCHIVE_REFRESH_MS = 5 * 60 * 1000L  // إعادة فحص الأرشيف الكامل

val TYPE_ORDER = listOf("official", "early", "breakout", "experimental")
val TYPE_LABEL = mapOf(
    "official" to "رسمية",
    "early" to "مبكرة",
    "breakout" to "انفجار",
    "experimental" to "تجريبية"
)

val TABS = listOf(
    "📈" to "الصفقات",
    "📊" to "الأداء",
    "📑" to "التقارير",
    "🧪" to "المحاكي",
    "💼" to "المحفظة"
)

// يُحدَّث من MainActivity: لا نستهلك الشبكة والتطبيق في الخلفية
object Fg {
    var on by mutableStateOf(true)
}

// ---------------- النماذج ----------------
data class OpenPos(
    val symbol: String, val type: String, val entry: Double,
    val sl: Double, val tp1: Double, val openedAt: String
)

data class Trade(
    val symbol: String, val type: String, val entry: Double,
    val exit: Double, val reason: String, val closedAt: String
) {
    // نفس حساب scanner.py: (خروج − دخول)/دخول × 100 − العمولة
    val net: Double?
        get() = if (entry > 0 && exit > 0) (exit - entry) / entry * 100.0 - FEE_PCT else null
}

data class Summary(
    val n: Int, val wins: Int, val losses: Int, val flat: Int,
    val avg: Double, val avgWin: Double, val avgLoss: Double,
    val pf: Double, val ciLow: Double, val ciHigh: Double
)

// ---- أقسام app_reports.json ----
data class SectionInfo(val ok: Boolean, val stale: Boolean, val error: String, val updatedAt: String)

data class TextSection(val info: SectionInfo, val messages: List<String>, val notes: List<String>)

data class TypeSim(val type: String, val count: Int, val profit: Double, val wins: Int, val losses: Int)

data class Preset(
    val key: String, val label: String, val text: String,
    val n: Int, val wins: Int, val losses: Int, val skipped: Int,
    val capital: Double, val finalBalance: Double, val totalProfit: Double,
    val maxConcurrent: Int, val byType: List<TypeSim>
)

data class SimSection(val info: SectionInfo, val presets: List<Preset>)

data class PaperRow(
    val label: String, val n: Int, val wr: Double, val mean: Double,
    val totalUsd: Double, val cap: Double
)

data class PaperSection(
    val info: SectionInfo, val messages: List<String>, val rows: List<PaperRow>,
    val available: Double, val initial: Double, val realized: Double, val openCount: Int
)

data class Reports(
    val generatedAt: String,
    val tradeStats: TextSection?,
    val regime: TextSection?,
    val sim: SimSection?,
    val paper: PaperSection?
)

data class Snapshot(
    val open: List<OpenPos>,
    val prices: Map<String, Double>,
    val trades: List<Trade>,
    val activeCount: Int,
    val archiveCount: Int,
    val archiveFiles: Int,
    val gists: Int,
    val reports: Reports?,
    val reportsError: String,
    val loadedAt: Long
)

// ---------------- التحليل الإحصائي ----------------
fun summarize(list: List<Trade>): Summary {
    val v = list.mapNotNull { it.net }
    val n = v.size
    if (n == 0) return Summary(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    val w = v.filter { it > BREAKEVEN_BAND }
    val l = v.filter { it < -BREAKEVEN_BAND }
    val avg = v.average()
    val sd = if (n > 1) sqrt(v.sumOf { (it - avg) * (it - avg) } / (n - 1)) else 0.0
    val half = if (n > 1) 1.96 * sd / sqrt(n.toDouble()) else 0.0
    val pos = v.filter { it > 0 }.sum()
    val neg = -v.filter { it < 0 }.sum()
    val pf = if (neg > 0) pos / neg else if (pos > 0) Double.POSITIVE_INFINITY else 0.0
    return Summary(
        n, w.size, l.size, n - w.size - l.size, avg,
        if (w.isEmpty()) 0.0 else w.average(),
        if (l.isEmpty()) 0.0 else l.average(),
        pf, avg - half, avg + half
    )
}

// ---------------- قراءة Gist (الأرشيف الكامل بنفس تنظيم scanner.py) ----------------
object Repo {
    private class Cached(val etag: String, val body: String)

    private val cache = HashMap<String, Cached>()
    private val tradeMemo = HashMap<String, Pair<String, List<Trade>>>()

    private var cfgKey = ""
    private var archiveReady = false
    private var archiveKey = ""
    private var archiveAt = 0L
    private var archiveTrades: List<Trade> = emptyList()
    private var archiveFileCount = 0
    private var archiveGistCount = 0
    private var lastPrices: Map<String, Double> = emptyMap()
    private var repRaw = ""
    private var repParsed: Reports? = null
    private var repError = ""

    // طلب مع ETag: الاستجابة 304 لا تحمّل البيانات ولا تُحسب من حد GitHub API
    private fun http(url: String, token: String?, cacheable: Boolean = true): String {
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.connectTimeout = 20000
            c.readTimeout = 30000
            c.setRequestProperty("User-Agent", "MarketScannerApp")
            if (url.contains("api.github.com")) c.setRequestProperty("Accept", "application/vnd.github+json")
            if (token != null) c.setRequestProperty("Authorization", "token $token")
            val cached = if (cacheable) synchronized(cache) { cache[url] } else null
            if (cached != null) c.setRequestProperty("If-None-Match", cached.etag)
            val code = c.responseCode
            if (code == 304 && cached != null) return cached.body
            if (code !in 200..299) {
                val hint = when (code) {
                    401 -> " (التوكن غير صالح)"
                    403, 429 -> " (حد الطلبات أو صلاحيات ناقصة)"
                    404 -> " (المعرّف خاطئ أو لا توجد صلاحية)"
                    else -> ""
                }
                throw Exception("HTTP $code$hint — ${URL(url).host}")
            }
            val body = c.inputStream.bufferedReader().use { it.readText() }
            val etag = c.getHeaderField("ETag")
            if (cacheable && etag != null) synchronized(cache) { cache[url] = Cached(etag, body) }
            return body
        } finally {
            c.disconnect()
        }
    }

    private fun gistFiles(id: String, token: String): JSONObject =
        JSONObject(http("https://api.github.com/gists/$id", token)).getJSONObject("files")

    // الملفات الكبيرة قد يقطعها GitHub API (truncated) → نجلبها كاملة من raw_url
    private fun content(f: JSONObject): String =
        if (f.optBoolean("truncated")) http(f.getString("raw_url"), null)
        else f.optString("content")

    private fun parseTrades(raw: String): List<Trade> {
        if (raw.isBlank()) return emptyList()
        val a = JSONArray(raw)
        return (0 until a.length()).map { i ->
            val o = a.getJSONObject(i)
            Trade(
                o.optString("symbol"),
                o.optString("type", "official").ifBlank { "official" },
                o.optDouble("entry", Double.NaN),
                o.optDouble("exit_price", Double.NaN),
                o.optString("closed_reason", "UNKNOWN"),
                o.optString("closed_at")
            )
        }
    }

    // نفس النص = نفس النتيجة، فلا نعيد التحليل عند 304
    private fun parseTradesMemo(key: String, raw: String): List<Trade> {
        val m = tradeMemo[key]
        if (m != null && m.first == raw) return m.second
        val t = parseTrades(raw)
        tradeMemo[key] = Pair(raw, t)
        return t
    }

    private fun parseOpen(raw: String): List<OpenPos> {
        if (raw.isBlank()) return emptyList()
        val a = JSONArray(raw)
        return (0 until a.length()).map { i ->
            val o = a.getJSONObject(i)
            OpenPos(
                o.optString("symbol"),
                o.optString("type", "official").ifBlank { "official" },
                o.optDouble("entry", Double.NaN),
                o.optDouble("sl", Double.NaN),
                o.optJSONArray("tps")?.optDouble(0, Double.NaN) ?: Double.NaN,
                o.optString("opened_at")
            )
        }
    }

    // ---- app_reports.json ----
    private fun infoOf(o: JSONObject) = SectionInfo(
        o.optBoolean("ok", false), o.optBoolean("stale", false),
        o.optString("error", ""), o.optString("updated_at", "")
    )

    private fun textList(o: JSONObject, key: String): List<String> {
        val v = o.opt(key)
        if (v is JSONArray) {
            val out = ArrayList<String>()
            for (i in 0 until v.length()) {
                val s = v.optString(i, "")
                if (s.isNotBlank()) out.add(s)
            }
            return out
        }
        if (v is String && v.isNotBlank()) return listOf(v)
        return emptyList()
    }

    private fun textSection(o: JSONObject?): TextSection? {
        if (o == null) return null
        return TextSection(infoOf(o), textList(o, "messages"), textList(o, "notes"))
    }

    private fun simSection(o: JSONObject?): SimSection? {
        if (o == null) return null
        val list = ArrayList<Preset>()
        val arr = o.optJSONArray("presets")
        if (arr != null) {
            for (i in 0 until arr.length()) {
                val p = arr.optJSONObject(i) ?: continue
                val r = p.optJSONObject("res") ?: JSONObject()
                val types = ArrayList<TypeSim>()
                val bt = r.optJSONObject("by_type")
                if (bt != null) {
                    val ks = bt.keys()
                    while (ks.hasNext()) {
                        val k = ks.next()
                        val v = bt.optJSONObject(k) ?: continue
                        types.add(
                            TypeSim(k, v.optInt("count"), v.optDouble("profit", 0.0), v.optInt("wins"), v.optInt("losses"))
                        )
                    }
                }
                list.add(
                    Preset(
                        p.optString("key"), p.optString("label"), p.optString("text"),
                        r.optInt("n"), r.optInt("wins"), r.optInt("losses"), r.optInt("skipped"),
                        r.optDouble("capital", 0.0), r.optDouble("final_balance", 0.0),
                        r.optDouble("total_profit", 0.0), r.optInt("max_concurrent"), types
                    )
                )
            }
        }
        return SimSection(infoOf(o), list)
    }

    private fun paperSection(o: JSONObject?): PaperSection? {
        if (o == null) return null
        val rows = ArrayList<PaperRow>()
        val arr = o.optJSONArray("compare")
        if (arr != null) {
            for (i in 0 until arr.length()) {
                val r = arr.optJSONObject(i) ?: continue
                rows.add(
                    PaperRow(
                        r.optString("label"), r.optInt("n"), r.optDouble("wr", 0.0),
                        r.optDouble("mean", 0.0), r.optDouble("total_usd", 0.0), r.optDouble("cap", 0.0)
                    )
                )
            }
        }
        val b = o.optJSONObject("balance") ?: JSONObject()
        return PaperSection(
            infoOf(o), textList(o, "messages"), rows,
            b.optDouble("available", 0.0), b.optDouble("initial_capital", 0.0),
            b.optDouble("realized_pnl_usd", 0.0), b.optInt("open_count")
        )
    }

    private fun parseReports(raw: String): Reports {
        val root = JSONObject(raw)
        val secs = root.optJSONObject("sections") ?: JSONObject()
        return Reports(
            root.optString("generated_at", ""),
            textSection(secs.optJSONObject("trade_stats")),
            textSection(secs.optJSONObject("market_regime")),
            simSection(secs.optJSONObject("simulator")),
            paperSection(secs.optJSONObject("paper"))
        )
    }

    // الأرشيف الكامل: كل Gists السلسلة + الـGist الرئيسي (قد يحوي ملفات أرشيف بنفسه).
    // أي فشل بجلب أي Gist يوقف التحميل بدل عرض بيانات ناقصة بصمت (والنسخة السابقة تبقى كما هي).
    private fun loadArchive(gistId: String, token: String, main: JSONObject, chainRaw: String) {
        val chain: List<String> =
            if (chainRaw.isBlank()) emptyList()
            else JSONArray(chainRaw).let { a -> (0 until a.length()).map { a.getString(it) } }
        val ids = (chain + gistId).distinct()
        val archive = ArrayList<Trade>()
        var files = 0
        for (id in ids) {
            val fs = if (id == gistId) main else gistFiles(id, token)
            val names = fs.keys().asSequence().filter { it.startsWith(ARCHIVE_PREFIX) }.sorted().toList()
            for (nm in names) {
                archive.addAll(parseTradesMemo("$id/$nm", content(fs.getJSONObject(nm))))
                files++
            }
        }
        archiveTrades = archive
        archiveFileCount = files
        archiveGistCount = ids.size
    }

    @Synchronized
    fun load(gistId: String, token: String, forceArchive: Boolean): Snapshot {
        val key = "$gistId|$token"
        if (key != cfgKey) {
            cfgKey = key
            synchronized(cache) { cache.clear() }
            tradeMemo.clear()
            archiveReady = false
            archiveKey = ""
            archiveTrades = emptyList()
            lastPrices = emptyMap()
            repRaw = ""
            repParsed = null
            repError = ""
        }

        val main = gistFiles(gistId, token)
        fun txt(name: String): String = main.optJSONObject(name)?.let { content(it) } ?: ""

        val open = parseOpen(txt(OPEN_FILE))
        val active = parseTradesMemo("active", txt(ACTIVE_FILE))

        val chainRaw = txt(CHAIN_FILE)
        val now = System.currentTimeMillis()
        if (forceArchive || !archiveReady || chainRaw != archiveKey || now - archiveAt > ARCHIVE_REFRESH_MS) {
            loadArchive(gistId, token, main, chainRaw)
            archiveKey = chainRaw
            archiveAt = now
            archiveReady = true
        }
        val all = (archiveTrades + active).sortedBy { it.closedAt }

        // التقارير: فشل قراءتها لا يمنع بقية الشاشات
        val raw = txt(REPORTS_FILE)
        if (raw != repRaw) {
            repRaw = raw
            if (raw.isBlank()) {
                repParsed = null
                repError = "الملف $REPORTS_FILE غير موجود في الـ Gist بعد."
            } else {
                try {
                    repParsed = parseReports(raw)
                    repError = ""
                } catch (e: Exception) {
                    repParsed = null
                    repError = "تعذّر قراءة $REPORTS_FILE: ${e.message}"
                }
            }
        }

        // الأسعار الحالية اختيارية — لو فشلت نعرض آخر أسعار ناجحة
        val syms = open.map { it.symbol }.distinct()
        if (syms.isNotEmpty()) {
            try {
                val q = URLEncoder.encode(JSONArray(syms).toString(), "UTF-8")
                val arr = JSONArray(http("https://data-api.binance.vision/api/v3/ticker/price?symbols=$q", null, false))
                val p = HashMap<String, Double>()
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    p[o.getString("symbol")] = o.getString("price").toDouble()
                }
                lastPrices = p
            } catch (e: Exception) {
                // نبقي آخر أسعار معروفة
            }
        }
        val prices = lastPrices.filterKeys { it in syms }

        return Snapshot(
            open, prices, all, active.size, archiveTrades.size, archiveFileCount, archiveGistCount,
            repParsed, repError, now
        )
    }
}

// ---------------- أدوات التنسيق ----------------
fun pct(v: Double): String = String.format(Locale.US, "%+.2f%%", v)
fun num(v: Double): String = if (v.isNaN()) "—" else String.format(Locale.US, "%.6g", v)
fun usd(v: Double): String = String.format(Locale.US, "%+.2f\$", v)
fun usdPlain(v: Double): String = String.format(Locale.US, "%.2f\$", v)
fun pctColor(v: Double): Color = if (v > 0) Color(0xFF66BB6A) else if (v < 0) Color(0xFFEF5350) else Color.Unspecified

fun clock(ms: Long): String = SimpleDateFormat("HH:mm:ss", Locale.US).format(Date(ms))

fun localTime(iso: String): String {
    if (iso.isBlank()) return "—"
    return try {
        val p = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
        p.timeZone = TimeZone.getTimeZone("UTC")
        val d = p.parse(iso) ?: return iso
        SimpleDateFormat("MM-dd HH:mm", Locale.US).format(d)
    } catch (e: Exception) {
        iso
    }
}

fun isSepLine(l: String): Boolean {
    val t = l.trim()
    return t.isNotEmpty() && t.all { it == '━' || it == '─' || it == '═' || it == '-' || it == '=' || it == '_' || it == '—' }
}

fun cleanBody(m: String): String = m.lines().filterNot { isSepLine(it) }.joinToString("\n").trim()

fun titleOf(m: String): String {
    val l = m.lines().firstOrNull { it.isNotBlank() && !isSepLine(it) } ?: return "تقرير"
    val t = l.trim()
    return if (t.length > 70) t.take(70) + "…" else t
}

// ---------------- الواجهة ----------------
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Rtl) {
                    Surface(Modifier.fillMaxSize()) {
                        Column(Modifier.fillMaxSize().systemBarsPadding()) { App() }
                    }
                }
            }
        }
    }

    override fun onStart() {
        super.onStart()
        Fg.on = true
    }

    override fun onStop() {
        Fg.on = false
        super.onStop()
    }
}

@Composable
fun App() {
    val ctx = LocalContext.current
    val prefs = remember { ctx.getSharedPreferences("cfg", Context.MODE_PRIVATE) }
    var gistId by remember { mutableStateOf(prefs.getString("gist_id", "") ?: "") }
    var token by remember { mutableStateOf(prefs.getString("token", "") ?: "") }
    var editing by remember { mutableStateOf(gistId.isBlank() || token.isBlank()) }

    if (editing) {
        Setup(
            gistId, token,
            canCancel = gistId.isNotBlank() && token.isNotBlank(),
            onCancel = { editing = false }
        ) { g, t ->
            prefs.edit().putString("gist_id", g.trim()).putString("token", t.trim()).apply()
            gistId = g.trim()
            token = t.trim()
            editing = false
        }
    } else {
        Dashboard(gistId, token) { editing = true }
    }
}

@Composable
fun Setup(gistId: String, token: String, canCancel: Boolean, onCancel: () -> Unit, onSave: (String, String) -> Unit) {
    var g by remember { mutableStateOf(gistId) }
    var t by remember { mutableStateOf(token) }
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("الإعدادات", style = MaterialTheme.typography.titleLarge)
        Text("تُحفظ هذه القيم داخل التطبيق على هاتفك فقط.")
        OutlinedTextField(g, { g = it }, label = { Text("GIST_ID") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(
            t, { t = it }, label = { Text("GIST_TOKEN") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth()
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { onSave(g, t) }, enabled = g.isNotBlank() && t.isNotBlank()) { Text("حفظ") }
            if (canCancel) TextButton(onClick = onCancel) { Text("إلغاء") }
        }
    }
}

@Composable
fun Dashboard(gistId: String, token: String, onSettings: () -> Unit) {
    var snap by remember { mutableStateOf<Snapshot?>(null) }
    var err by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(true) }
    var tab by remember { mutableIntStateOf(0) }
    var manual by remember { mutableIntStateOf(0) }

    // حلقة التحديث التلقائي: تتوقف في الخلفية، وتحدّث فوراً عند العودة للتطبيق.
    // إعادة تشغيلها (زر تحديث أو تغيير الإعدادات) تفرض إعادة فحص الأرشيف الكامل.
    LaunchedEffect(gistId, token, manual) {
        var since = REFRESH_SECONDS
        var first = true
        while (true) {
            if (Fg.on) {
                if (since >= REFRESH_SECONDS) {
                    since = 0
                    busy = true
                    try {
                        val force = first
                        first = false
                        snap = withContext(Dispatchers.IO) { Repo.load(gistId, token, force) }
                        err = ""
                    } catch (e: CancellationException) {
                        throw e
                    } catch (e: Exception) {
                        err = e.message ?: e.toString()
                    }
                    busy = false
                }
                since++
            } else {
                since = REFRESH_SECONDS
            }
            delay(1000)
        }
    }

    val cur = snap
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 8.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            TextButton(onClick = { manual++ }) { Text("تحديث") }
            Text(
                if (busy) "جارٍ التحديث…" else if (cur != null) "آخر تحديث ${clock(cur.loadedAt)}" else "",
                style = MaterialTheme.typography.bodySmall
            )
            TextButton(onClick = onSettings) { Text("الإعدادات") }
        }
        if (err.isNotEmpty()) {
            Text(
                "تعذّر التحديث: $err" + if (cur != null) " — تُعرض آخر بيانات ناجحة" else "",
                Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
                color = Color(0xFFEF5350),
                style = MaterialTheme.typography.bodySmall
            )
        }
        Box(Modifier.weight(1f).fillMaxWidth()) {
            if (cur == null) {
                if (err.isEmpty()) Text("جارٍ التحميل…", Modifier.padding(16.dp))
            } else {
                when (tab) {
                    0 -> TradesScreen(cur)
                    1 -> PerfScreen(cur)
                    2 -> ReportsScreen(cur)
                    3 -> SimulatorScreen(cur)
                    else -> PaperScreen(cur)
                }
            }
        }
        NavigationBar {
            TABS.forEachIndexed { i, (icon, label) ->
                NavigationBarItem(
                    selected = tab == i,
                    onClick = { tab = i },
                    icon = { Text(icon) },
                    label = { Text(label, maxLines = 1, fontSize = 11.sp) }
                )
            }
        }
    }
}

@Composable
fun Line(a: String, b: String, color: Color = Color.Unspecified) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(a)
        Text(b, color = color, fontWeight = FontWeight.Medium)
    }
}

@Composable
fun SectionNotice(info: SectionInfo) {
    if (!info.ok) {
        Text("⚠️ القسم غير متاح حالياً: ${info.error.ifBlank { "لم يُنشر بعد" }}", color = Color(0xFFEF5350))
    } else if (info.stale) {
        Text(
            "⚠️ بيانات قديمة (آخر نجاح ${localTime(info.updatedAt)}) — السبب: ${info.error}",
            color = Color(0xFFFFB74D)
        )
    } else {
        Text("آخر تحديث للقسم: ${localTime(info.updatedAt)}", style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
fun NoReports(err: String) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Text("التقارير غير متاحة بعد", fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(4.dp))
            Text(err.ifBlank { "شغّل workflow باسم Export App Data مرة واحدة يدوياً من تبويب Actions." })
        }
    }
}

@Composable
fun ReportCard(title: String, body: String) {
    var open by remember { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth().clickable { open = !open }) {
        Column(Modifier.padding(12.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text(title, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                Text(if (open) "▲" else "▼")
            }
            if (open) {
                Spacer(Modifier.height(8.dp))
                Text(body)
            }
        }
    }
}

fun LazyListScope.messageItems(prefix: String, messages: List<String>) {
    itemsIndexed(messages, key = { i, _ -> prefix + "-m" + i }) { _, m ->
        ReportCard(titleOf(m), cleanBody(m))
    }
}

fun LazyListScope.textSectionItems(prefix: String, heading: String, sec: TextSection?) {
    item(key = prefix + "-h") {
        Text(heading, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.titleMedium)
    }
    if (sec == null) {
        item(key = prefix + "-none") { Text("القسم غير موجود في التقرير.") }
        return
    }
    item(key = prefix + "-i") { SectionNotice(sec.info) }
    messageItems(prefix, sec.messages)
    if (sec.notes.isNotEmpty()) {
        item(key = prefix + "-n") {
            Text(sec.notes.joinToString("\n"), style = MaterialTheme.typography.bodySmall)
        }
    }
}

// ---- 1) الصفقات ----
@Composable
fun TradesScreen(snap: Snapshot) {
    if (snap.open.isEmpty()) {
        Text("لا توجد صفقات مفتوحة حالياً.", Modifier.padding(16.dp))
        return
    }
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        item { Text("عدد الصفقات المفتوحة: ${snap.open.size}") }
        items(snap.open) { p ->
            val price = snap.prices[p.symbol]
            val chg = if (price != null && p.entry > 0) (price - p.entry) / p.entry * 100.0 else null
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text(
                        "${p.symbol.removeSuffix("USDT")}/USDT — ${TYPE_LABEL[p.type] ?: p.type}",
                        fontWeight = FontWeight.Bold
                    )
                    Line("الدخول", num(p.entry))
                    Line("TP1", num(p.tp1))
                    Line("SL", num(p.sl))
                    if (price != null) Line("السعر الحالي", num(price))
                    if (chg != null) Line("التغير", pct(chg), pctColor(chg))
                    Line("فُتحت", p.openedAt)
                }
            }
        }
    }
}

// ---- 2) الأداء (الإحصائيات + P&L لكل نوع، من الأرشيف الكامل) ----
@Composable
fun SummaryLines(s: Summary) {
    if (s.n == 0) {
        Text("لا توجد صفقات.")
        return
    }
    val wr = s.wins * 100.0 / s.n
    Line("عدد الصفقات", "${s.n}")
    Line("فوز / خسارة / متعادل", "${s.wins} / ${s.losses} / ${s.flat}")
    Line("نسبة النجاح", String.format(Locale.US, "%.1f%%", wr))
    Line("متوسط صافي/صفقة", pct(s.avg), pctColor(s.avg))
    Line("متوسط الربح", pct(s.avgWin), pctColor(s.avgWin))
    Line("متوسط الخسارة", pct(s.avgLoss), pctColor(s.avgLoss))
    Line("Profit Factor", if (s.pf.isInfinite()) "∞" else String.format(Locale.US, "%.2f", s.pf))
    Line("فاصل 95%", "${pct(s.ciLow)} → ${pct(s.ciHigh)}")
}

@Composable
fun PerfScreen(snap: Snapshot) {
    val overall = remember(snap) { summarize(snap.trades) }
    val byType = remember(snap) { snap.trades.groupBy { it.type } }
    val types = TYPE_ORDER.filter { byType.containsKey(it) } + byType.keys.filter { it !in TYPE_ORDER }
    val reasons = remember(snap) { snap.trades.groupingBy { it.reason }.eachCount().entries.sortedByDescending { it.value } }
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        item {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text("البيانات المحمّلة", fontWeight = FontWeight.Bold)
                    Line("إجمالي الصفقات المغلقة", "${snap.trades.size}")
                    Line("journal نشط", "${snap.activeCount}")
                    Line("أرشيف", "${snap.archiveCount} (${snap.archiveFiles} ملف / ${snap.gists} Gist)")
                }
            }
        }
        item {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text("الأداء الكلي (صافي بعد عمولة ${FEE_PCT}%)", fontWeight = FontWeight.Bold)
                    SummaryLines(overall)
                }
            }
        }
        items(types) { t ->
            val s = summarize(byType[t] ?: emptyList())
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text(TYPE_LABEL[t] ?: t, fontWeight = FontWeight.Bold)
                    Spacer(Modifier.height(4.dp))
                    SummaryLines(s)
                }
            }
        }
        item {
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(12.dp)) {
                    Text("أسباب الإغلاق", fontWeight = FontWeight.Bold)
                    reasons.forEach { Line(it.key, "${it.value}") }
                }
            }
        }
    }
}

// ---- 3) التقارير (trade_stats + market_regime_report) ----
@Composable
fun ReportsScreen(snap: Snapshot) {
    val rep = snap.reports
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        if (rep == null) {
            item { NoReports(snap.reportsError) }
        } else {
            item { Text("آخر تصدير: ${localTime(rep.generatedAt)}", style = MaterialTheme.typography.bodySmall) }
            textSectionItems("ts", "📊 تقرير الصفقات (trade_stats)", rep.tradeStats)
            textSectionItems("mr", "🌐 نظام السوق (market_regime)", rep.regime)
        }
    }
}

// ---- 4) المحاكي ----
@Composable
fun SimulatorScreen(snap: Snapshot) {
    val rep = snap.reports
    val sec = rep?.sim
    var sel by remember { mutableIntStateOf(0) }
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        if (rep == null) {
            item { NoReports(snap.reportsError) }
        } else if (sec == null) {
            item { Text("قسم المحاكي غير موجود في التقرير.") }
        } else {
            item { SectionNotice(sec.info) }
            if (sec.presets.isEmpty()) {
                item { Text("لا توجد نتائج محاكاة متاحة.") }
            } else {
                val idx = sel.coerceIn(0, sec.presets.size - 1)
                val p = sec.presets[idx]
                item {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        sec.presets.forEachIndexed { i, pr ->
                            if (i == idx) {
                                Button(
                                    onClick = { sel = i },
                                    modifier = Modifier.weight(1f),
                                    contentPadding = PaddingValues(horizontal = 4.dp)
                                ) { Text(pr.label, maxLines = 1, fontSize = 12.sp) }
                            } else {
                                OutlinedButton(
                                    onClick = { sel = i },
                                    modifier = Modifier.weight(1f),
                                    contentPadding = PaddingValues(horizontal = 4.dp)
                                ) { Text(pr.label, maxLines = 1, fontSize = 12.sp) }
                            }
                        }
                    }
                }
                item {
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(12.dp)) {
                            Text("ملخص المحاكاة — ${p.label}", fontWeight = FontWeight.Bold)
                            Line("رأس المال", usdPlain(p.capital))
                            Line("الرصيد النهائي", usdPlain(p.finalBalance), pctColor(p.totalProfit))
                            Line("إجمالي الربح", usd(p.totalProfit), pctColor(p.totalProfit))
                            Line("صفقات منفّذة", "${p.n}")
                            Line("فوز / خسارة", "${p.wins} / ${p.losses}")
                            if (p.skipped > 0) Line("صفقات متخطّاة", "${p.skipped}")
                            Line("أقصى صفقات متزامنة", "${p.maxConcurrent}")
                            if (p.byType.isNotEmpty()) {
                                Spacer(Modifier.height(6.dp))
                                Text("حسب النوع", fontWeight = FontWeight.Bold)
                                p.byType.forEach { ts ->
                                    Line(
                                        "${TYPE_LABEL[ts.type] ?: ts.type} (${ts.count})",
                                        usd(ts.profit), pctColor(ts.profit)
                                    )
                                }
                            }
                        }
                    }
                }
                item { ReportCard("التقرير الكامل", cleanBody(p.text)) }
            }
        }
    }
}

// ---- 5) المحفظة الوهمية ----
@Composable
fun PaperScreen(snap: Snapshot) {
    val rep = snap.reports
    val sec = rep?.paper
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        if (rep == null) {
            item { NoReports(snap.reportsError) }
        } else if (sec == null) {
            item { Text("قسم المحفظة الوهمية غير موجود في التقرير.") }
        } else {
            item { SectionNotice(sec.info) }
            item {
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp)) {
                        Text("المحفظة الرئيسية", fontWeight = FontWeight.Bold)
                        Line("الرصيد المتاح", usdPlain(sec.available))
                        Line("رأس المال الابتدائي", usdPlain(sec.initial))
                        Line("ربح/خسارة محقّق", usd(sec.realized), pctColor(sec.realized))
                        Line("صفقات مفتوحة", "${sec.openCount}")
                    }
                }
            }
            if (sec.rows.isNotEmpty()) {
                item { Text("🏆 مقارنة المحافظ (حسب الربح بالدولار)", fontWeight = FontWeight.Bold) }
                val ranked = sec.rows.sortedWith(
                    compareBy<PaperRow> { if (it.n == 0) 1 else 0 }.thenByDescending { it.totalUsd }
                )
                itemsIndexed(ranked, key = { i, _ -> "pf$i" }) { i, r ->
                    Card(Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(12.dp)) {
                            Text("${i + 1}. ${r.label}", fontWeight = FontWeight.Bold)
                            if (r.n == 0) {
                                Text("لا صفقات مغلقة بعد")
                            } else {
                                val roi = if (r.cap > 0) r.totalUsd / r.cap * 100.0 else 0.0
                                Line("عدد الصفقات", "${r.n}")
                                Line("نسبة النجاح", String.format(Locale.US, "%.0f%%", r.wr))
                                Line("متوسط صافي/صفقة", pct(r.mean), pctColor(r.mean))
                                Line(
                                    "الربح",
                                    "${usd(r.totalUsd)} (${pct(roi)} من ${usdPlain(r.cap)})",
                                    pctColor(r.totalUsd)
                                )
                            }
                        }
                    }
                }
            }
            if (sec.messages.isNotEmpty()) {
                item { Text("📄 التقارير التفصيلية", fontWeight = FontWeight.Bold) }
                messageItems("pp", sec.messages)
            }
        }
    }
}
