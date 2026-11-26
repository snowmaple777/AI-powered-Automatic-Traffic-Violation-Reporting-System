package tw.nuk.autoform

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.util.concurrent.Callable
import java.util.concurrent.Executors

/** 在手機目前的 Wi-Fi 網段自動找出開著「手機展示模式」的分析電腦，使用者不必輸入網址。 */
object Discovery {
    const val PORT = 8765
    private const val PREFS = "analysis-host"

    fun saved(context: Context) = context.getSharedPreferences(PREFS, 0).getString("address", null)
    fun remember(context: Context, address: String) =
        context.getSharedPreferences(PREFS, 0).edit().putString("address", address).apply()

    /** 依序：上次成功的位址 → 掃描同網段。找不到回傳 null。必須在背景執行緒呼叫。 */
    fun find(context: Context): ProjectConnection? {
        saved(context)?.let { address ->
            runCatching { ProjectConnection.parse(address) }.getOrNull()?.let { if (verify(it)) return it }
        }
        // 每個網段只掃手機所在的 /24（家用 Wi-Fi 與手機熱點常見大小），避免大網段掃太久。
        val locals = localAddresses(context)
        android.util.Log.i("Discovery", "scan subnets: " + locals.joinToString { it.hostAddress.orEmpty().substringBeforeLast('.') + ".0/24" })
        val hosts = locals.flatMap { own ->
            val octets = own.address.map { it.toInt() and 255 }
            val net = octets.take(3).joinToString(".")
            (1..254).filter { it != octets[3] }.map { "$net.$it" }
        }.distinct()
        if (hosts.isEmpty()) return null
        val pool = Executors.newFixedThreadPool(64)
        try {
            val open = pool.invokeAll(hosts.map { ip -> Callable { ip.takeIf { portOpen(it) } } })
                .mapNotNull { runCatching { it.get() }.getOrNull() }
            for (ip in open) {
                val server = runCatching { ProjectConnection.parse("http://$ip:$PORT") }.getOrNull() ?: continue
                if (verify(server)) return server
            }
        } finally { pool.shutdownNow() }
        return null
    }

    private fun portOpen(ip: String, port: Int = PORT) = runCatching {
        Socket().use { it.connect(InetSocketAddress(ip, port), 500); true }
    }.getOrDefault(false)

    private fun verify(server: ProjectConnection) =
        portOpen(server.host, server.port) && runCatching { server.checkService(); true }.getOrDefault(false)

    /**
     * 手機在區網上的 IPv4 位址：
     * 1. 以用戶端身分連上的 Wi-Fi／有線網路；
     * 2. 手機自己開的行動熱點或 USB 網路共用（不是 ConnectivityManager 的 Network，
     *    只能從網路介面讀到，例如 Samsung 的 swlan0、ap0、rndis0）。
     * 行動數據介面（rmnet、ccmni 等）不掃：那是電信業者的網段，不會有分析電腦。
     */
    internal fun localAddresses(context: Context): List<Inet4Address> {
        val found = mutableListOf<Inet4Address>()
        val cm = context.getSystemService(ConnectivityManager::class.java)
        if (cm != null) {
            for (network in (listOfNotNull(cm.activeNetwork) + cm.allNetworks.toList()).distinct()) {
                val caps = cm.getNetworkCapabilities(network) ?: continue
                if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) && !caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) continue
                val link = cm.getLinkProperties(network) ?: continue
                link.linkAddresses.filter { it.prefixLength >= 16 }.map { it.address }
                    .filterIsInstance<Inet4Address>().filter(::isPrivate).forEach(found::add)
            }
        }
        val cellular = Regex("^(rmnet|r_rmnet|v4-rmnet|ccmni|clat|seth|pdp|ppp|dummy|lo).*")
        runCatching {
            for (nic in NetworkInterface.getNetworkInterfaces().toList()) {
                if (!nic.isUp || nic.isLoopback || cellular.matches(nic.name)) continue
                nic.interfaceAddresses.filter { it.networkPrefixLength in 16..30 }.map { it.address }
                    .filterIsInstance<Inet4Address>().filter(::isPrivate).forEach(found::add)
            }
        }
        return found.distinctBy { it.hostAddress }
    }

    private fun isPrivate(ip: Inet4Address): Boolean {
        val (a, b) = ip.address.take(2).map { it.toInt() and 255 }
        return a == 10 || a == 192 && b == 168 || a == 172 && b in 16..31
    }
}
