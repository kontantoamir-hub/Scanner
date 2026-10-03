package com.scanner.dashboard

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.systemBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextDirection
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

val TABS = listOf("الصفقات", "الأداء", "التقارير", "المحاكي", "المحفظة")

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
fun pctColor(v: Double): Color = if (v > 0) Pal.Up else if (v < 0) Pal.Down else Color.Unspecified

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

// ---------------- الثيم (Bloomberg-style) ----------------
object Pal {
    val Bg = Color(0xFF0B0D10)
    val Card = Color(0xFF14171B)
    val Line = Color(0xFF23272D)
    val Text = Color(0xFFE6E8EB)
    val Muted = Color(0xFF8A9099)
    val Amber = Color(0xFFFFB000)
    val Up = Color(0xFF2EBD85)
    val Down = Color(0xFFF6465D)
}

val NumStyle = TextStyle(fontFamily = FontFamily.Monospace, textDirection = TextDirection.Ltr)

fun cv(c: Color): Color = if (c == Color.Unspecified) Pal.Text else c

val AppColors = darkColorScheme(
    primary = Pal.Amber,
    onPrimary = Color.Black,
    background = Pal.Bg,
    onBackground = Pal.Text,
    surface = Pal.Bg,
    onSurface = Pal.Text,
    surfaceVariant = Pal.Card,
    onSurfaceVariant = Pal.Muted,
    outline = Pal.Line,
    error = Pal.Down
)

// ---------------- الواجهة ----------------
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT)
        )
        setContent {
            MaterialTheme(colorScheme = AppColors) {
                CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Rtl) {
                    Surface(Modifier.fillMaxSize(), color = Pal.Bg) {
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
    val fieldColors = OutlinedTextFieldDefaults.colors(
        focusedBorderColor = Pal.Amber,
        unfocusedBorderColor = Pal.Line,
        focusedLabelColor = Pal.Amber,
        unfocusedLabelColor = Pal.Muted,
        cursorColor = Pal.Amber,
        focusedTextColor = Pal.Text,
        unfocusedTextColor = Pal.Text
    )
    Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Text("MARKET SCANNER", color = Pal.Amber, fontSize = 13.sp, fontWeight = FontWeight.Medium, letterSpacing = 1.5.sp)
        Text("الإعدادات", fontSize = 24.sp, fontWeight = FontWeight.Medium)
        Text("تُحفظ هذه القيم داخل التطبيق على هاتفك فقط.", color = Pal.Muted, fontSize = 13.sp)
        OutlinedTextField(
            g, { g = it }, label = { Text("GIST_ID") }, singleLine = true,
            colors = fieldColors, shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()
        )
        OutlinedTextField(
            t, { t = it }, label = { Text("GIST_TOKEN") }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            colors = fieldColors, shape = RoundedCornerShape(8.dp), modifier = Modifier.fillMaxWidth()
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(
                onClick = { onSave(g, t) },
                enabled = g.isNotBlank() && t.isNotBlank(),
                shape = RoundedCornerShape(8.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = Pal.Amber, contentColor = Color.Black,
                    disabledContainerColor = Pal.Line, disabledContentColor = Pal.Muted
                )
            ) { Text("حفظ", fontWeight = FontWeight.Medium) }
            if (canCancel) TextButton(onClick = onCancel) { Text("إلغاء", color = Pal.Muted) }
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
    val dot = if (err.isNotEmpty()) Pal.Down else if (busy) Pal.Amber else Pal.Up
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(7.dp).clip(CircleShape).background(dot))
                Spacer(Modifier.width(8.dp))
                Column {
                    Text("MARKET SCANNER", color = Pal.Amber, fontSize = 13.sp, fontWeight = FontWeight.Medium, letterSpacing = 1.5.sp)
                    Text(
                        if (busy) "جارٍ التحديث…" else if (cur != null) "آخر تحديث ${clock(cur.loadedAt)}" else "",
                        color = Pal.Muted, fontSize = 11.sp
                    )
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
                HeaderChip("تحديث") { manual++ }
                HeaderChip("الإعدادات", onSettings)
            }
        }
        Box(Modifier.fillMaxWidth().height(0.5.dp).background(Pal.Line))
        if (err.isNotEmpty()) {
            Text(
                "تعذّر التحديث: $err" + if (cur != null) " — تُعرض آخر بيانات ناجحة" else "",
                color = Pal.Down, fontSize = 12.sp,
                modifier = Modifier.fillMaxWidth().background(Pal.Down.copy(alpha = 0.12f)).padding(horizontal = 16.dp, vertical = 8.dp)
            )
        }
        Box(Modifier.weight(1f).fillMaxWidth()) {
            if (cur == null) {
                if (err.isEmpty()) Text("جارٍ التحميل…", color = Pal.Muted, modifier = Modifier.padding(16.dp))
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
        BottomBar(tab) { tab = it }
    }
}

// ---------------- مكونات مشتركة ----------------
@Composable
fun HeaderChip(text: String, onClick: () -> Unit) {
    val shape = RoundedCornerShape(6.dp)
    Text(
        text, color = Pal.Text, fontSize = 12.sp,
        modifier = Modifier.clip(shape).border(0.5.dp, Pal.Line, shape).clickable(onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 6.dp)
    )
}

@Composable
fun Panel(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
    val shape = RoundedCornerShape(8.dp)
    Column(
        modifier.fillMaxWidth().clip(shape).background(Pal.Card).border(0.5.dp, Pal.Line, shape).padding(12.dp),
        content = content
    )
}

@Composable
fun Heading(text: String) {
    Text(text, color = Pal.Amber, fontSize = 13.sp, fontWeight = FontWeight.Medium, letterSpacing = 0.5.sp)
}

@Composable
fun Line(a: String, b: String, color: Color = Color.Unspecified) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 5.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(a, color = Pal.Muted, fontSize = 13.sp, modifier = Modifier.weight(1f))
        Text(b, color = cv(color), fontSize = 13.sp, fontWeight = FontWeight.Medium, style = NumStyle)
    }
}

@Composable
fun Stat(label: String, value: String, modifier: Modifier = Modifier, color: Color = Color.Unspecified) {
    Column(modifier) {
        Text(label, color = Pal.Muted, fontSize = 11.sp)
        Text(value, color = cv(color), fontSize = 14.sp, fontWeight = FontWeight.Medium, style = NumStyle)
    }
}

@Composable
fun Tile(label: String, value: String, modifier: Modifier = Modifier, color: Color = Color.Unspecified) {
    val shape = RoundedCornerShape(8.dp)
    Column(
        modifier.clip(shape).background(Pal.Card).border(0.5.dp, Pal.Line, shape)
            .padding(horizontal = 12.dp, vertical = 10.dp)
    ) {
        Text(label, color = Pal.Muted, fontSize = 11.sp)
        Text(value, color = cv(color), fontSize = 18.sp, fontWeight = FontWeight.Medium, style = NumStyle)
    }
}

@Composable
fun TypeTag(text: String) {
    val shape = RoundedCornerShape(4.dp)
    Text(
        text, color = Pal.Amber, fontSize = 10.sp,
        modifier = Modifier.border(0.5.dp, Pal.Amber.copy(alpha = 0.6f), shape).padding(horizontal = 6.dp, vertical = 2.dp)
    )
}

@Composable
fun Seg(text: String, on: Boolean, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val shape = RoundedCornerShape(6.dp)
    Box(
        modifier.clip(shape)
            .background(if (on) Pal.Amber else Color.Transparent)
            .border(0.5.dp, if (on) Pal.Amber else Pal.Line, shape)
            .clickable(onClick = onClick)
            .padding(vertical = 8.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(text, color = if (on) Color.Black else Pal.Muted, fontSize = 12.sp, fontWeight = FontWeight.Medium, maxLines = 1)
    }
}

@Composable
fun EquityCurve(values: List<Double>, modifier: Modifier = Modifier) {
    Canvas(modifier) {
        val w = size.width
        val h = size.height
        for (g in 1..3) {
            val y = h * g / 4f
            drawLine(Pal.Line, Offset(0f, y), Offset(w, y), 1f)
        }
        if (values.size < 2) return@Canvas
        val step = maxOf(1, values.size / 240)
        val pts = ArrayList<Double>()
        var idx = 0
        while (idx < values.size) {
            pts.add(values[idx])
            idx += step
        }
        if ((values.size - 1) % step != 0) pts.add(values.last())
        val lo = minOf(pts.minOrNull() ?: 0.0, 0.0)
        val hi = maxOf(pts.maxOrNull() ?: 0.0, 0.0)
        val rng = if (hi - lo < 1e-9) 1.0 else hi - lo
        val pad = 6.dp.toPx()
        fun yOf(v: Double): Float = (h - pad - ((v - lo) / rng * (h - 2 * pad))).toFloat()
        fun xOf(k: Int): Float = w * k / (pts.size - 1).toFloat()
        val zy = yOf(0.0)
        drawLine(Pal.Muted.copy(alpha = 0.35f), Offset(0f, zy), Offset(w, zy), 1f)
        val path = Path()
        pts.forEachIndexed { k, v ->
            if (k == 0) path.moveTo(xOf(k), yOf(v)) else path.lineTo(xOf(k), yOf(v))
        }
        drawPath(path, Pal.Amber, style = Stroke(width = 2.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round))
        drawCircle(Pal.Amber, 3.5.dp.toPx(), Offset(xOf(pts.size - 1), yOf(pts.last())))
    }
}

@Composable
fun RangeBar(sl: Double, entry: Double, price: Double, tp: Double) {
    val span = tp - sl
    val fp = ((price - sl) / span).coerceIn(0.0, 1.0).toFloat()
    val fe = ((entry - sl) / span).coerceIn(0.0, 1.0).toFloat()
    val col = if (price >= entry) Pal.Up else Pal.Down
    CompositionLocalProvider(LocalLayoutDirection provides LayoutDirection.Ltr) {
        Column {
            Canvas(Modifier.fillMaxWidth().height(12.dp)) {
                val w = size.width
                val cy = size.height / 2f
                val th = 3.dp.toPx()
                drawLine(Pal.Line, Offset(0f, cy), Offset(w, cy), th, StrokeCap.Round)
                drawLine(col, Offset(w * fe, cy), Offset(w * fp, cy), th, StrokeCap.Round)
                drawLine(Pal.Muted, Offset(w * fe, cy - 5.dp.toPx()), Offset(w * fe, cy + 5.dp.toPx()), 1.dp.toPx())
                drawCircle(Pal.Amber, 5.dp.toPx(), Offset(w * fp, cy))
            }
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Text("SL", color = Pal.Down, fontSize = 10.sp)
                Text("TP1", color = Pal.Up, fontSize = 10.sp)
            }
        }
    }
}

@Composable
fun NavIcon(kind: Int, color: Color) {
    Canvas(Modifier.size(22.dp)) {
        val s = size.width
        val sw = 1.8.dp.toPx()
        val st = Stroke(width = sw, cap = StrokeCap.Round, join = StrokeJoin.Round)
        when (kind) {
            0 -> repeat(3) { i ->
                val y = s * (0.26f + 0.24f * i)
                drawLine(color, Offset(s * 0.38f, y), Offset(s * 0.9f, y), strokeWidth = sw, cap = StrokeCap.Round)
                drawCircle(color, s * 0.055f, Offset(s * 0.14f, y))
            }
            1 -> {
                val p = Path()
                p.moveTo(s * 0.1f, s * 0.72f)
                p.lineTo(s * 0.38f, s * 0.46f)
                p.lineTo(s * 0.58f, s * 0.62f)
                p.lineTo(s * 0.9f, s * 0.24f)
                drawPath(p, color, style = st)
            }
            2 -> {
                drawRoundRect(
                    color, topLeft = Offset(s * 0.2f, s * 0.1f), size = Size(s * 0.6f, s * 0.8f),
                    cornerRadius = CornerRadius(2.dp.toPx()), style = st
                )
                drawLine(color, Offset(s * 0.34f, s * 0.38f), Offset(s * 0.66f, s * 0.38f), strokeWidth = sw, cap = StrokeCap.Round)
                drawLine(color, Offset(s * 0.34f, s * 0.58f), Offset(s * 0.66f, s * 0.58f), strokeWidth = sw, cap = StrokeCap.Round)
            }
            3 -> {
                val p = Path()
                p.moveTo(s * 0.4f, s * 0.1f)
                p.lineTo(s * 0.4f, s * 0.42f)
                p.lineTo(s * 0.15f, s * 0.85f)
                p.lineTo(s * 0.85f, s * 0.85f)
                p.lineTo(s * 0.6f, s * 0.42f)
                p.lineTo(s * 0.6f, s * 0.1f)
                drawPath(p, color, style = st)
                drawLine(color, Offset(s * 0.32f, s * 0.1f), Offset(s * 0.68f, s * 0.1f), strokeWidth = sw, cap = StrokeCap.Round)
            }
            else -> {
                drawRoundRect(
                    color, topLeft = Offset(s * 0.1f, s * 0.24f), size = Size(s * 0.8f, s * 0.56f),
                    cornerRadius = CornerRadius(3.dp.toPx()), style = st
                )
                drawCircle(color, s * 0.05f, Offset(s * 0.72f, s * 0.52f))
            }
        }
    }
}

@Composable
fun BottomBar(sel: Int, onSel: (Int) -> Unit) {
    Column {
        Box(Modifier.fillMaxWidth().height(0.5.dp).background(Pal.Line))
        Row(Modifier.fillMaxWidth().background(Pal.Bg)) {
            TABS.forEachIndexed { i, label ->
                val on = sel == i
                val c = if (on) Pal.Amber else Pal.Muted
                Column(
                    Modifier.weight(1f).clickable { onSel(i) },
                    horizontalAlignment = Alignment.CenterHorizontally
                ) {
                    Box(Modifier.fillMaxWidth().height(2.dp).background(if (on) Pal.Amber else Color.Transparent))
                    Spacer(Modifier.height(8.dp))
                    NavIcon(i, c)
                    Spacer(Modifier.height(3.dp))
                    Text(label, color = c, fontSize = 10.sp, maxLines = 1)
                    Spacer(Modifier.height(8.dp))
                }
            }
        }
    }
}

@Composable
fun SectionNotice(info: SectionInfo) {
    if (!info.ok) {
        Text("القسم غير متاح حالياً: ${info.error.ifBlank { "لم يُنشر بعد" }}", color = Pal.Down, fontSize = 12.sp)
    } else if (info.stale) {
        Text(
            "بيانات قديمة (آخر نجاح ${localTime(info.updatedAt)}) — السبب: ${info.error}",
            color = Pal.Amber, fontSize = 12.sp
        )
    } else {
        Text("آخر تحديث للقسم: ${localTime(info.updatedAt)}", color = Pal.Muted, fontSize = 11.sp)
    }
}

@Composable
fun NoReports(err: String) {
    Panel {
        Text("التقارير غير متاحة بعد", fontWeight = FontWeight.Medium)
        Spacer(Modifier.height(4.dp))
        Text(
            err.ifBlank { "شغّل workflow باسم Export App Data مرة واحدة يدوياً من تبويب Actions." },
            color = Pal.Muted, fontSize = 13.sp
        )
    }
}

@Composable
fun ReportCard(title: String, body: String) {
    var open by remember { mutableStateOf(false) }
    Panel(Modifier.clickable { open = !open }) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(title, fontWeight = FontWeight.Medium, fontSize = 14.sp, modifier = Modifier.weight(1f))
            Text(if (open) "▴" else "▾", color = Pal.Amber)
        }
        if (open) {
            Spacer(Modifier.height(8.dp))
            Text(body, fontSize = 12.sp, lineHeight = 18.sp, fontFamily = FontFamily.Monospace)
        }
    }
}

fun LazyListScope.messageItems(prefix: String, messages: List<String>) {
    itemsIndexed(messages, key = { i, _ -> prefix + "-m" + i }) { _, m ->
        ReportCard(titleOf(m), cleanBody(m))
    }
}

fun LazyListScope.textSectionItems(prefix: String, heading: String, sec: TextSection?) {
    item(key = prefix + "-h") { Heading(heading) }
    if (sec == null) {
        item(key = prefix + "-none") { Text("القسم غير موجود في التقرير.", color = Pal.Muted) }
        return
    }
    item(key = prefix + "-i") { SectionNotice(sec.info) }
    messageItems(prefix, sec.messages)
    if (sec.notes.isNotEmpty()) {
        item(key = prefix + "-n") {
            Text(sec.notes.joinToString("\n"), color = Pal.Muted, fontSize = 11.sp)
        }
    }
}

// ---- 1) الصفقات ----
@Composable
fun PositionCard(p: OpenPos, price: Double?) {
    val chg = if (price != null && p.entry > 0) (price - p.entry) / p.entry * 100.0 else null
    Panel {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(p.symbol.removeSuffix("USDT"), fontSize = 16.sp, fontWeight = FontWeight.Medium, style = NumStyle)
                Text("/USDT", color = Pal.Muted, fontSize = 12.sp, style = NumStyle)
                Spacer(Modifier.width(8.dp))
                TypeTag(TYPE_LABEL[p.type] ?: p.type)
            }
            Text(
                if (chg != null) pct(chg) else "—",
                color = if (chg != null) cv(pctColor(chg)) else Pal.Muted,
                fontSize = 18.sp, fontWeight = FontWeight.Medium, style = NumStyle
            )
        }
        if (price != null && !p.sl.isNaN() && !p.tp1.isNaN() && p.tp1 > p.sl) {
            Spacer(Modifier.height(10.dp))
            RangeBar(p.sl, p.entry, price, p.tp1)
        }
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth()) {
            Stat("الدخول", num(p.entry), Modifier.weight(1f))
            Stat("السعر الحالي", if (price != null) num(price) else "—", Modifier.weight(1f))
        }
        Spacer(Modifier.height(6.dp))
        Row(Modifier.fillMaxWidth()) {
            Stat("TP1", num(p.tp1), Modifier.weight(1f), Pal.Up)
            Stat("SL", num(p.sl), Modifier.weight(1f), Pal.Down)
        }
        Spacer(Modifier.height(6.dp))
        Text("فُتحت ${p.openedAt}", color = Pal.Muted, fontSize = 11.sp)
    }
}

@Composable
fun TradesScreen(snap: Snapshot) {
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        item {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 4.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Heading("الصفقات المفتوحة")
                Text("${snap.open.size}", color = Pal.Amber, fontSize = 13.sp, style = NumStyle)
            }
        }
        if (snap.open.isEmpty()) {
            item { Panel { Text("لا توجد صفقات مفتوحة حالياً.", color = Pal.Muted, fontSize = 13.sp) } }
        }
        items(snap.open) { p -> PositionCard(p, snap.prices[p.symbol]) }
    }
}

// ---- 2) الأداء (الإحصائيات + P&L لكل نوع، من الأرشيف الكامل) ----
@Composable
fun SummaryLines(s: Summary) {
    if (s.n == 0) {
        Text("لا توجد صفقات.", color = Pal.Muted, fontSize = 13.sp)
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
    val curve = remember(snap) {
        var acc = 0.0
        val out = ArrayList<Double>()
        out.add(0.0)
        for (t in snap.trades) {
            val n = t.net ?: continue
            acc += n
            out.add(acc)
        }
        out
    }
    val total = curve.last()
    val byType = remember(snap) { snap.trades.groupBy { it.type } }
    val types = TYPE_ORDER.filter { byType.containsKey(it) } + byType.keys.filter { it !in TYPE_ORDER }
    val reasons = remember(snap) { snap.trades.groupingBy { it.reason }.eachCount().entries.sortedByDescending { it.value } }
    val maxC = reasons.maxOfOrNull { it.value } ?: 1
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        item {
            Panel {
                Text("الأداء الكلي (صافي بعد عمولة ${FEE_PCT}%)", color = Pal.Muted, fontSize = 12.sp)
                Text(pct(total), color = cv(pctColor(total)), fontSize = 30.sp, fontWeight = FontWeight.Medium, style = NumStyle)
                Text("مجموع صافي نسب كل الصفقات المغلقة", color = Pal.Muted, fontSize = 11.sp)
                Spacer(Modifier.height(10.dp))
                EquityCurve(curve, Modifier.fillMaxWidth().height(110.dp))
            }
        }
        if (overall.n > 0) {
            val wr = overall.wins * 100.0 / overall.n
            item {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Tile("نسبة النجاح", String.format(Locale.US, "%.1f%%", wr), Modifier.weight(1f))
                    Tile(
                        "Profit Factor",
                        if (overall.pf.isInfinite()) "∞" else String.format(Locale.US, "%.2f", overall.pf),
                        Modifier.weight(1f)
                    )
                }
            }
            item {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Tile("عدد الصفقات", "${overall.n}", Modifier.weight(1f))
                    Tile("متوسط صافي/صفقة", pct(overall.avg), Modifier.weight(1f), pctColor(overall.avg))
                }
            }
        }
        item {
            Panel {
                Heading("التفاصيل")
                Spacer(Modifier.height(4.dp))
                SummaryLines(overall)
            }
        }
        items(types) { t ->
            val s = summarize(byType[t] ?: emptyList())
            Panel {
                Text(TYPE_LABEL[t] ?: t, fontWeight = FontWeight.Medium)
                Spacer(Modifier.height(4.dp))
                SummaryLines(s)
            }
        }
        item {
            Panel {
                Heading("أسباب الإغلاق")
                Spacer(Modifier.height(6.dp))
                reasons.forEach { r ->
                    Column(Modifier.padding(vertical = 4.dp)) {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                            Text(r.key, fontSize = 13.sp)
                            Text("${r.value}", fontSize = 13.sp, style = NumStyle)
                        }
                        Spacer(Modifier.height(3.dp))
                        Box(Modifier.fillMaxWidth().height(3.dp).background(Pal.Line)) {
                            Box(
                                Modifier.fillMaxWidth(r.value.toFloat() / maxC.toFloat()).height(3.dp)
                                    .background(Pal.Amber.copy(alpha = 0.8f))
                            )
                        }
                    }
                }
            }
        }
        item {
            Panel {
                Heading("البيانات المحمّلة")
                Spacer(Modifier.height(4.dp))
                Line("إجمالي الصفقات المغلقة", "${snap.trades.size}")
                Line("journal نشط", "${snap.activeCount}")
                Line("أرشيف", "${snap.archiveCount} (${snap.archiveFiles} ملف / ${snap.gists} Gist)")
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
            item { Text("آخر تصدير: ${localTime(rep.generatedAt)}", color = Pal.Muted, fontSize = 11.sp) }
            textSectionItems("ts", "تقرير الصفقات (trade_stats)", rep.tradeStats)
            textSectionItems("mr", "نظام السوق (market_regime)", rep.regime)
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
            item { Text("قسم المحاكي غير موجود في التقرير.", color = Pal.Muted) }
        } else {
            item { SectionNotice(sec.info) }
            if (sec.presets.isEmpty()) {
                item { Text("لا توجد نتائج محاكاة متاحة.", color = Pal.Muted) }
            } else {
                val idx = sel.coerceIn(0, sec.presets.size - 1)
                val p = sec.presets[idx]
                item {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        sec.presets.forEachIndexed { i, pr ->
                            Seg(pr.label, i == idx, Modifier.weight(1f)) { sel = i }
                        }
                    }
                }
                item {
                    Panel {
                        Text("الرصيد النهائي — ${p.label}", color = Pal.Muted, fontSize = 12.sp)
                        Text(usdPlain(p.finalBalance), fontSize = 28.sp, fontWeight = FontWeight.Medium, style = NumStyle)
                        Text(usd(p.totalProfit), color = cv(pctColor(p.totalProfit)), fontSize = 14.sp, style = NumStyle)
                        Spacer(Modifier.height(8.dp))
                        Line("رأس المال", usdPlain(p.capital))
                        Line("إجمالي الربح", usd(p.totalProfit), pctColor(p.totalProfit))
                        Line("صفقات منفّذة", "${p.n}")
                        Line("فوز / خسارة", "${p.wins} / ${p.losses}")
                        if (p.skipped > 0) Line("صفقات متخطّاة", "${p.skipped}")
                        Line("أقصى صفقات متزامنة", "${p.maxConcurrent}")
                    }
                }
                if (p.byType.isNotEmpty()) {
                    item {
                        Panel {
                            Heading("حسب النوع")
                            Spacer(Modifier.height(4.dp))
                            p.byType.forEach { ts ->
                                Line(
                                    "${TYPE_LABEL[ts.type] ?: ts.type} (${ts.count})",
                                    usd(ts.profit), pctColor(ts.profit)
                                )
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
            item { Text("قسم المحفظة الوهمية غير موجود في التقرير.", color = Pal.Muted) }
        } else {
            item { SectionNotice(sec.info) }
            item {
                Panel {
                    Text("المحفظة الرئيسية — الرصيد المتاح", color = Pal.Muted, fontSize = 12.sp)
                    Text(usdPlain(sec.available), fontSize = 28.sp, fontWeight = FontWeight.Medium, style = NumStyle)
                    Text(usd(sec.realized), color = cv(pctColor(sec.realized)), fontSize = 14.sp, style = NumStyle)
                    Spacer(Modifier.height(8.dp))
                    Line("رأس المال الابتدائي", usdPlain(sec.initial))
                    Line("ربح/خسارة محقّق", usd(sec.realized), pctColor(sec.realized))
                    Line("صفقات مفتوحة", "${sec.openCount}")
                }
            }
            if (sec.rows.isNotEmpty()) {
                item { Heading("مقارنة المحافظ (حسب الربح بالدولار)") }
                val ranked = sec.rows.sortedWith(
                    compareBy<PaperRow> { if (it.n == 0) 1 else 0 }.thenByDescending { it.totalUsd }
                )
                itemsIndexed(ranked, key = { i, _ -> "pf$i" }) { i, r ->
                    Panel {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("${i + 1}", color = Pal.Amber, fontSize = 15.sp, fontWeight = FontWeight.Medium, style = NumStyle)
                            Spacer(Modifier.width(10.dp))
                            Text(r.label, fontWeight = FontWeight.Medium)
                        }
                        Spacer(Modifier.height(4.dp))
                        if (r.n == 0) {
                            Text("لا صفقات مغلقة بعد", color = Pal.Muted, fontSize = 13.sp)
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
            if (sec.messages.isNotEmpty()) {
                item { Heading("التقارير التفصيلية") }
                messageItems("pp", sec.messages)
            }
        }
    }
}
