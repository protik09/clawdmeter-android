package local.clawdmeter;

import android.Manifest;
import android.app.Activity;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattServer;
import android.bluetooth.BluetoothGattServerCallback;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.AdvertiseData;
import android.bluetooth.le.AdvertiseSettings;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.ActivityNotFoundException;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Insets;
import android.net.Uri;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Build;
import android.os.Bundle;
import android.os.ParcelUuid;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;

/**
 * Fullscreen, keep-screen-on WebView that renders the Clawdmeter UI (assets/index.html).
 * Wi-Fi: JS polls every bridge it knows; this class finds them on the LAN (mDNS `_clawdmeter._tcp`).
 * Bluetooth: this phone acts as a Clawdmeter "board", a GATT server speaking the upstream protocol
 * (docs/protocol.md), so computers running the bridge (or upstream's app) write their payloads here.
 */
public class MainActivity extends Activity {
    static final UUID SVC = UUID.fromString("4c41555a-4465-7669-6365-000000000001");
    static final UUID RX = UUID.fromString("4c41555a-4465-7669-6365-000000000002");
    static final UUID TX = UUID.fromString("4c41555a-4465-7669-6365-000000000003");
    static final UUID REQ = UUID.fromString("4c41555a-4465-7669-6365-000000000004");
    static final UUID CCCD = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb");
    static final String[] PERMS = {Manifest.permission.BLUETOOTH_ADVERTISE, Manifest.permission.BLUETOOTH_CONNECT};

    WebView web;
    FrameLayout root;
    BluetoothManager bm;
    BluetoothGattServer gatt;
    BluetoothGattCharacteristic txChar, reqChar;
    boolean wantBle, advertiseName = true;
    volatile boolean gotData;
    final Set<BluetoothDevice> connected = ConcurrentHashMap.newKeySet();
    final Map<String, Set<UUID>> subs = new ConcurrentHashMap<>();
    final Map<String, ByteArrayOutputStream> pending = new ConcurrentHashMap<>();
    NsdManager nsd;
    NsdManager.DiscoveryListener disc;
    final Map<String, String> found = new ConcurrentHashMap<>(); // mDNS service name -> bridge URL
    final ArrayDeque<NsdServiceInfo> toResolve = new ArrayDeque<>(); // UI thread only
    boolean resolving;
    final ExecutorService probe = Executors.newCachedThreadPool();

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Window w = getWindow();
        w.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); // the whole point: never sleep while shown
        setShowWhenLocked(true); // desk display: after a power-button press, wake straight back to the meters
        setTurnScreenOn(true);
        w.setDecorFitsSystemWindows(false);
        WindowManager.LayoutParams lp = w.getAttributes(); // same edge-to-edge on 13 as on 15+, which enforce it
        lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_ALWAYS;
        lp.preferredRefreshRate = 60f; // a dashboard never needs 120 Hz; spare the battery and the panel
        w.setAttributes(lp);

        web = new WebView(this);
        web.setBackgroundColor(0xff000000);
        root = new FrameLayout(this);
        root.setBackgroundColor(0xff000000);
        root.addView(web);
        root.setOnApplyWindowInsetsListener((v, ins) -> { // keep UI clear of the camera hole
            Insets c = ins.getInsets(WindowInsets.Type.displayCutout());
            v.setPadding(c.left, c.top, c.right, c.bottom);
            return WindowInsets.CONSUMED;
        });
        setContentView(root);
        hideBars();

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setAllowFileAccessFromFileURLs(true); // fonts/sprites from file:///android_asset
        s.setMediaPlaybackRequiresUserGesture(false); // chimes without a tap
        web.addJavascriptInterface(new Js(), "Android");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean onRenderProcessGone(WebView v, RenderProcessGoneDetail d) {
                recreate(); // the renderer was killed (memory pressure): rebuild instead of closing the display
                return true;
            }
        });
        web.loadUrl("file:///android_asset/index.html");
        registerReceiver(btState, new IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED), Context.RECEIVER_NOT_EXPORTED);
    }

    /** Bluetooth switched off/on (or its stack restarted): the GATT server dies with it, so follow the adapter. */
    final BroadcastReceiver btState = new BroadcastReceiver() {
        @Override
        public void onReceive(Context c, Intent i) {
            int st = i.getIntExtra(BluetoothAdapter.EXTRA_STATE, -1);
            if (st == BluetoothAdapter.STATE_TURNING_OFF && gatt != null) { stopBle(); status("Bluetooth is off"); }
            else if (st == BluetoothAdapter.STATE_ON && wantBle) startBle();
        }
    };

    /** Run JS in the page, unless the activity is already gone. */
    void js(String code) {
        runOnUiThread(() -> { if (!isDestroyed()) web.evaluateJavascript(code, null); });
    }

    void hideBars() {
        WindowInsetsController c = getWindow().getInsetsController();
        if (c == null) return;
        c.hide(WindowInsets.Type.systemBars());
        c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
    }

    @Override
    public void onWindowFocusChanged(boolean focus) {
        super.onWindowFocusChanged(focus);
        if (focus) hideBars();
    }

    @Override
    protected void onDestroy() {
        unregisterReceiver(btState);
        stopBle();
        stopDiscovery();
        probe.shutdownNow();
        web.destroy();
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        for (int r : results) if (r != PackageManager.PERMISSION_GRANTED) { status("Bluetooth permission denied"); return; }
        if (wantBle) startBle();
    }

    class Js {
        @JavascriptInterface
        public void setBle(boolean on) {
            runOnUiThread(() -> {
                wantBle = on;
                if (on) startBle(); else stopBle();
            });
        }

        /** A found computer stopped answering: it may have a new address, so look again. */
        @JavascriptInterface
        public void rediscover() {
            runOnUiThread(() -> { if (disc != null) { stopDiscovery(); startDiscovery(); } });
        }

        /** The phone's own 12/24-hour setting (the page only sees the locale). */
        @JavascriptInterface
        public boolean is24h() {
            return android.text.format.DateFormat.is24HourFormat(MainActivity.this);
        }

        /** Wi-Fi mode: look for bridges on the LAN. */
        @JavascriptInterface
        public void setDiscovery(boolean on) {
            runOnUiThread(() -> { if (on) startDiscovery(); else stopDiscovery(); });
        }

        /** Tap ended an alert: tell the computer that raised it, as the board does. */
        @JavascriptInterface
        public void dismiss(String address) {
            byte[] v = "{\"dismiss\":true}".getBytes(StandardCharsets.UTF_8);
            for (BluetoothDevice d : connected) if (d.getAddress().equals(address)) notifyIfSubscribed(d, txChar, v);
        }

        /** Paint the camera-cutout margin in the page's background colour. */
        @JavascriptInterface
        public void setBg(String hex) {
            runOnUiThread(() -> { try { root.setBackgroundColor(android.graphics.Color.parseColor(hex)); } catch (IllegalArgumentException ignored) { } });
        }

        /** Open a web page (the bridge's download page) in the phone's browser. https only. */
        @JavascriptInterface
        public void openUrl(String url) {
            Uri u = Uri.parse(url);
            if (!"https".equals(u.getScheme())) return;
            runOnUiThread(() -> {
                try { startActivity(new Intent(Intent.ACTION_VIEW, u)); } catch (ActivityNotFoundException ignored) { }
            });
        }

        /** 0..1, or -1 for the system brightness. */
        @JavascriptInterface
        public void setBrightness(float v) {
            runOnUiThread(() -> {
                WindowManager.LayoutParams a = getWindow().getAttributes();
                a.screenBrightness = v;
                getWindow().setAttributes(a);
            });
        }
    }

    // ---- Bluetooth: pretend to be a Clawdmeter board -------------------------------------

    void startBle() {
        if (gatt != null) return;
        for (String p : PERMS) {
            if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) { requestPermissions(PERMS, 1); return; }
        }
        bm = getSystemService(BluetoothManager.class);
        BluetoothAdapter ad = bm == null ? null : bm.getAdapter();
        if (ad == null || !ad.isEnabled()) { status("Bluetooth is off"); return; }
        gatt = bm.openGattServer(this, cb);
        if (gatt == null) { status("GATT server unavailable"); return; }
        BluetoothGattService svc = new BluetoothGattService(SVC, BluetoothGattService.SERVICE_TYPE_PRIMARY);
        svc.addCharacteristic(new BluetoothGattCharacteristic(RX,
                BluetoothGattCharacteristic.PROPERTY_WRITE | BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE,
                BluetoothGattCharacteristic.PERMISSION_WRITE));
        svc.addCharacteristic(txChar = notifier(TX));
        svc.addCharacteristic(reqChar = notifier(REQ));
        gatt.addService(svc); // advertising starts in onServiceAdded
        status("starting");
    }

    static BluetoothGattCharacteristic notifier(UUID u) {
        BluetoothGattCharacteristic c = new BluetoothGattCharacteristic(u, BluetoothGattCharacteristic.PROPERTY_NOTIFY, 0);
        c.addDescriptor(new BluetoothGattDescriptor(CCCD,
                BluetoothGattDescriptor.PERMISSION_READ | BluetoothGattDescriptor.PERMISSION_WRITE));
        return c;
    }

    void stopBle() {
        if (gatt == null) return;
        BluetoothLeAdvertiser a = bm.getAdapter().getBluetoothLeAdvertiser();
        if (a != null) a.stopAdvertising(advCb);
        gatt.close();
        gatt = null;
        connected.clear();
        subs.clear();
        pending.clear();
        status("off");
    }

    void advertise() {
        if (gatt == null) return;
        BluetoothLeAdvertiser a = bm.getAdapter().getBluetoothLeAdvertiser();
        if (a == null) { status("BLE advertising unsupported"); return; }
        a.stopAdvertising(advCb);
        AdvertiseSettings set = new AdvertiseSettings.Builder()
                .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_BALANCED)
                .setConnectable(true).setTimeout(0).build();
        // The marker: a paired computer's BlueZ reports its cached service list, not the live advertisement,
        // so after Bluetooth restarts the service UUID alone may not identify this phone. 0xFFFF = test company ID.
        AdvertiseData data = new AdvertiseData.Builder().addServiceUuid(new ParcelUuid(SVC))
                .addManufacturerData(0xFFFF, "clwd".getBytes(StandardCharsets.US_ASCII)).build();
        AdvertiseData scan = new AdvertiseData.Builder().setIncludeDeviceName(advertiseName).build();
        a.startAdvertising(set, data, scan, advCb);
    }

    final AdvertiseCallback advCb = new AdvertiseCallback() {
        @Override
        public void onStartSuccess(AdvertiseSettings s) { status(connected.isEmpty() ? "waiting for a computer" : "connected"); }

        @Override
        public void onStartFailure(int err) {
            if (err == ADVERTISE_FAILED_DATA_TOO_LARGE && advertiseName) { advertiseName = false; advertise(); return; } // long phone name
            if (err != ADVERTISE_FAILED_ALREADY_STARTED) status("advertising failed (" + err + ")");
        }
    };

    /** Bluetooth state + every connected computer ({id, name}); JS asks which to show when there are several. */
    void status(String s) {
        JSONArray list = new JSONArray();
        for (BluetoothDevice d : connected) {
            String name = null;
            try { name = d.getName(); } catch (SecurityException ignored) { }
            try { list.put(new JSONObject().put("id", d.getAddress()).put("name", name == null ? "" : name)); } catch (JSONException ignored) { }
        }
        js("window.onBle&&onBle(" + JSONObject.quote(s) + "," + list + ")");
    }

    void deliver(BluetoothDevice d, byte[] v) {
        gotData = true;
        js("window.onPayload&&onPayload(" + JSONObject.quote(new String(v, StandardCharsets.UTF_8)) + ",'ble',"
                + JSONObject.quote(d.getAddress()) + ")");
    }

    void notifyIfSubscribed(BluetoothDevice d, BluetoothGattCharacteristic c, byte[] v) {
        BluetoothGattServer g = gatt;
        Set<UUID> s = subs.get(d.getAddress());
        if (g != null && c != null && s != null && s.contains(c.getUuid())) g.notifyCharacteristicChanged(d, c, false, v);
    }

    final BluetoothGattServerCallback cb = new BluetoothGattServerCallback() {
        @Override
        public void onServiceAdded(int st, BluetoothGattService s) { runOnUiThread(MainActivity.this::advertise); }

        @Override
        public void onConnectionStateChange(BluetoothDevice d, int st, int newState) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                connected.add(d);
                status("connected");
            } else {
                connected.remove(d);
                subs.remove(d.getAddress());
                pending.remove(d.getAddress());
                status(connected.isEmpty() ? "waiting for a computer" : "connected");
                runOnUiThread(MainActivity.this::advertise); // controllers stop advertising on connect
            }
        }

        @Override
        public void onCharacteristicWriteRequest(BluetoothDevice d, int id, BluetoothGattCharacteristic c,
                                                 boolean prepared, boolean respond, int offset, byte[] v) {
            if (prepared) pending.computeIfAbsent(d.getAddress(), k -> new ByteArrayOutputStream()).write(v, 0, v.length);
            else if (RX.equals(c.getUuid())) deliver(d, v);
            if (respond) gatt.sendResponse(d, id, BluetoothGatt.GATT_SUCCESS, offset, v);
        }

        @Override
        public void onExecuteWrite(BluetoothDevice d, int id, boolean execute) { // long (>MTU) writes
            ByteArrayOutputStream b = pending.remove(d.getAddress());
            if (execute && b != null) deliver(d, b.toByteArray());
            gatt.sendResponse(d, id, BluetoothGatt.GATT_SUCCESS, 0, null);
        }

        @Override
        public void onDescriptorWriteRequest(BluetoothDevice d, int id, BluetoothGattDescriptor desc,
                                             boolean prepared, boolean respond, int offset, byte[] v) {
            UUID ch = desc.getCharacteristic().getUuid();
            boolean on = v != null && v.length > 0 && v[0] != 0;
            Set<UUID> s = subs.computeIfAbsent(d.getAddress(), k -> ConcurrentHashMap.newKeySet());
            if (on) s.add(ch); else s.remove(ch);
            if (respond) gatt.sendResponse(d, id, BluetoothGatt.GATT_SUCCESS, offset, v);
            // Like the board: ask for a poll when subscribed before any data arrived.
            if (on && REQ.equals(ch) && !gotData) notifyIfSubscribed(d, reqChar, new byte[]{1});
        }

        @Override
        public void onDescriptorReadRequest(BluetoothDevice d, int id, int offset, BluetoothGattDescriptor desc) {
            Set<UUID> s = subs.get(d.getAddress());
            boolean on = s != null && s.contains(desc.getCharacteristic().getUuid());
            gatt.sendResponse(d, id, BluetoothGatt.GATT_SUCCESS, 0,
                    on ? BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE : BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE);
        }
    };

    // ---- Wi-Fi: find bridges on the LAN (mDNS / DNS-SD) ----------------------------------

    void startDiscovery() {
        if (disc != null) return;
        nsd = getSystemService(NsdManager.class);
        disc = new NsdManager.DiscoveryListener() {
            @Override public void onServiceFound(NsdServiceInfo si) { runOnUiThread(() -> watch(si)); }
            @Override public void onServiceLost(NsdServiceInfo si) { runOnUiThread(() -> unwatch(si.getServiceName())); }
            @Override public void onDiscoveryStarted(String type) { }
            @Override public void onDiscoveryStopped(String type) { }
            @Override public void onStartDiscoveryFailed(String type, int err) { disc = null; }
            @Override public void onStopDiscoveryFailed(String type, int err) { }
        };
        nsd.discoverServices("_clawdmeter._tcp", NsdManager.PROTOCOL_DNS_SD, disc);
    }

    void stopDiscovery() {
        if (disc == null) return;
        try { nsd.stopServiceDiscovery(disc); } catch (IllegalArgumentException ignored) { }
        disc = null;
        toResolve.clear();
        if (!found.isEmpty()) { found.clear(); pushBridges(); }
    }

    void watch(NsdServiceInfo si) {
        toResolve.add(si);
        resolveNext();
    }

    /** resolveService works from Android 13 on, but there only one resolve may run at a time: queue them. */
    void resolveNext() {
        if (resolving || disc == null || toResolve.isEmpty()) return;
        resolving = true;
        nsd.resolveService(toResolve.poll(), new NsdManager.ResolveListener() {
            @Override public void onServiceResolved(NsdServiceInfo r) {
                try { probe.execute(() -> reach(r.getServiceName(), r)); } catch (RejectedExecutionException ignored) { } // closing
                next();
            }
            @Override public void onResolveFailed(NsdServiceInfo s, int err) { next(); }
            void next() { runOnUiThread(() -> { resolving = false; resolveNext(); }); }
        });
    }

    void unwatch(String name) {
        if (found.remove(name) != null) pushBridges();
    }

    /** A computer announces every address it has (Ethernet, Wi-Fi, VPN, Docker...): use one the phone can reach. */
    void reach(String name, NsdServiceInfo r) {
        List<InetAddress> addrs = Build.VERSION.SDK_INT >= 34 ? r.getHostAddresses() : List.of(r.getHost()); // 13: one address
        for (InetAddress a : addrs) {
            if (!(a instanceof Inet4Address)) continue; // ponytail: IPv4 only; IPv6 needs [addr%scope] URLs
            try (Socket so = new Socket()) {
                so.connect(new InetSocketAddress(a, r.getPort()), 1500);
                String url = "http://" + a.getHostAddress() + ":" + r.getPort();
                if (!url.equals(found.put(name, url))) pushBridges();
                return;
            } catch (IOException ignored) { }
        }
    }

    void pushBridges() {
        JSONArray list = new JSONArray();
        for (Map.Entry<String, String> e : found.entrySet()) {
            try { list.put(new JSONObject().put("name", e.getKey()).put("url", e.getValue())); } catch (JSONException ignored) { }
        }
        js("window.onBridges&&onBridges(" + list + ")");
    }
}
