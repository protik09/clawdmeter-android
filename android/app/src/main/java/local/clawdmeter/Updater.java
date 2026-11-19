package local.clawdmeter;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInstaller;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Self-update from GitHub Releases: checks at launch and once a day, and installs a newer release's APK over this
 * app. Since Android 12 an app may update itself without a prompt when the user has let it install apps
 * ("Install unknown apps") and the new APK is signed with the same key, which is why release CI signs every
 * release with one permanent key.
 * Installing closes the app. Android reopens it when it's the Home app (kiosk mode); otherwise UpdateReceiver tries,
 * and leaves a notification to tap.
 */
final class Updater {
    static final String PREFS = "updates", CHANNEL = "updates";
    static final int NOTE_ID = 1, REQ_NOTIF = 2;
    static final long HOUR = 3600_000L, DAY = 24 * HOUR, LAUNCH_GAP = 10 * 60_000L;
    static final Pattern SEMVER = Pattern.compile("^v?(\\d+)\\.(\\d+)\\.(\\d+)");
    static volatile Updater live; // the running activity's updater, for UpdateReceiver

    final MainActivity a;
    final SharedPreferences prefs;
    final Handler h = new Handler(Looper.getMainLooper());
    final ExecutorService io = Executors.newSingleThreadExecutor();
    final boolean dev = !SEMVER.matcher(BuildConfig.VERSION_NAME).find(); // a local build: no updates
    // Written on the UI thread; volatile because the page reads them from WebView's bridge thread (info()).
    volatile String status = "idle", latest, error; // idle | checking | available | downloading | installing | confirm
    volatile int pct;
    String url, digest;
    long size;
    boolean launched, wantInstall, closed;

    Updater(MainActivity a) {
        this.a = a;
        prefs = a.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    void start() {
        live = this;
        // Running again: whatever install was in flight is over, and the "tap to open" notification has done its job.
        prefs.edit().remove("installing").apply();
        a.getSystemService(NotificationManager.class).cancel(NOTE_ID);
        if (!dev) h.postDelayed(tick, 15_000); // let the page settle first
    }

    void stop() {
        closed = true;
        if (live == this) live = null;
        h.removeCallbacksAndMessages(null);
        io.shutdownNow();
    }

    /** Hourly; checks at launch (unless the app restarted minutes ago), then once a day, retrying hourly on failure. */
    final Runnable tick = new Runnable() {
        @Override
        public void run() {
            long t = System.currentTimeMillis(), tried = prefs.getLong("triedAt", 0);
            boolean due = launched ? t - prefs.getLong("checkedAt", 0) >= DAY && t - tried >= HOUR : t - tried >= LAUNCH_GAP;
            launched = true;
            if (auto() && due) check();
            h.postDelayed(this, HOUR);
        }
    };

    boolean auto() { return prefs.getBoolean("auto", true); }

    boolean canInstall() { return a.getPackageManager().canRequestPackageInstalls(); }

    /** The notification permission hasn't been asked for yet (asked once, with the install permission). */
    boolean askNotif() {
        return a.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                && !prefs.getBoolean("askedNotif", false);
    }

    boolean busy() { return !status.equals("idle") && !status.equals("available"); }

    // ---- from the page --------------------------------------------------------------------

    void setAuto(boolean on) {
        prefs.edit().putBoolean("auto", on).apply();
        if (on && !dev) check(); else push();
    }

    /** "Update" in the menu: install now, retrying a version that failed before. */
    void installNow() {
        if (latest == null) return;
        wantInstall = true;
        error = null;
        prefs.edit().remove("failed").remove("failedWhy").apply();
        if (canInstall()) install(); else allow();
    }

    /** Ask for the notification permission (once), then send the user to "Install unknown apps" for this app. */
    void allow() {
        if (askNotif()) {
            prefs.edit().putBoolean("askedNotif", true).apply();
            a.requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF); // -> afterNotif()
            return;
        }
        if (canInstall()) { push(); return; }
        try {
            a.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + a.getPackageName())));
        } catch (ActivityNotFoundException ignored) { }
    }

    void afterNotif() {
        if (canInstall()) push(); else allow();
    }

    /** Back from the system settings: if that granted the install permission, go on with a waiting update. */
    void onResume() {
        if (dev || closed) return;
        if (status.equals("available") && canInstall() && (wantInstall || (auto() && error == null))) install();
        else push();
    }

    // ---- checking ---------------------------------------------------------------------------

    /** Ask GitHub for the latest release (drafts and pre-releases excluded); install it when newer and wanted. */
    void check() {
        if (dev || busy()) return;
        status = "checking";
        error = null;
        push();
        prefs.edit().putLong("triedAt", System.currentTimeMillis()).apply();
        io.execute(() -> {
            try {
                JSONObject r = new JSONObject(get(BuildConfig.UPDATE_FEED));
                String tag = r.getString("tag_name");
                Matcher m = SEMVER.matcher(tag);
                if (!m.find()) throw new JSONException("not a version: " + tag);
                // the same versionCode release CI derives from the tag
                int code = Integer.parseInt(m.group(1)) * 10000 + Integer.parseInt(m.group(2)) * 100 + Integer.parseInt(m.group(3));
                String apk = null, sha = null;
                long sz = 0;
                JSONArray as = r.getJSONArray("assets");
                for (int i = 0; i < as.length(); i++) {
                    JSONObject o = as.getJSONObject(i);
                    if (!o.getString("name").endsWith(".apk")) continue;
                    apk = o.getString("browser_download_url");
                    sz = o.optLong("size");
                    String d = o.optString("digest"); // "sha256:<hex>", on assets uploaded since mid-2025
                    sha = d.startsWith("sha256:") ? d.substring(7) : null;
                    break;
                }
                String v = tag.replaceFirst("^v", ""), u = apk, s = sha;
                long n = sz;
                h.post(() -> found(v, code, u, n, s));
            } catch (IOException | JSONException | RuntimeException e) {
                h.post(() -> {
                    status = latest != null ? "available" : "idle";
                    error = "Couldn't check for updates";
                    push();
                });
            }
        });
    }

    void found(String v, int code, String apk, long sz, String sha) {
        if (closed) return;
        prefs.edit().putLong("checkedAt", System.currentTimeMillis()).apply();
        if (code <= BuildConfig.VERSION_CODE || apk == null) {
            status = "idle";
            latest = null;
            push();
            return;
        }
        latest = v;
        url = apk;
        size = sz;
        digest = sha;
        status = "available";
        // A version that failed to install isn't retried by itself (that would download it again every day).
        if (v.equals(prefs.getString("failed", null))) {
            error = prefs.getString("failedWhy", null);
            push();
        } else if (auto() || wantInstall) install();
        else push();
    }

    // ---- installing -------------------------------------------------------------------------

    void install() {
        if (latest == null || busy() || closed) return;
        if (!canInstall()) { status = "available"; push(); return; } // the menu asks for the permission
        status = "downloading";
        pct = 0;
        error = null;
        push();
        String v = latest, u = url, sha = digest;
        long sz = size;
        io.execute(() -> {
            PackageInstaller pi = a.getPackageManager().getPackageInstaller();
            int id = -1;
            try {
                // GitHub serves https; a test feed (-PupdateFeed=http://...) may serve plain http
                if (!u.startsWith("https://") && BuildConfig.UPDATE_FEED.startsWith("https://")) throw new IOException("not https: " + u);
                PackageInstaller.SessionParams sp = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
                sp.setAppPackageName(a.getPackageName());
                sp.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED);
                if (sz > 0) sp.setSize(sz);
                id = pi.createSession(sp);
                try (PackageInstaller.Session s = pi.openSession(id)) {
                    MessageDigest md = MessageDigest.getInstance("SHA-256");
                    HttpURLConnection c = open(u);
                    try (InputStream in = c.getInputStream(); OutputStream out = s.openWrite("base.apk", 0, c.getContentLengthLong())) {
                        long total = c.getContentLengthLong(), done = 0;
                        byte[] buf = new byte[64 * 1024];
                        int n, last = -1;
                        while ((n = in.read(buf)) > 0) {
                            if (closed) throw new IOException("closing");
                            out.write(buf, 0, n);
                            md.update(buf, 0, n);
                            done += n;
                            int p = total > 0 ? (int) (100 * done / total) : 0;
                            if (p != last) { last = p; h.post(() -> { pct = p; push(); }); }
                        }
                        s.fsync(out);
                    } finally {
                        c.disconnect();
                    }
                    if (sha != null && !sha.equalsIgnoreCase(hex(md.digest()))) throw new IOException("checksum mismatch");
                    if (closed) throw new IOException("closing");
                    // Marks this as our own update, for UpdateReceiver (not adb or a file manager). commit(), not
                    // apply(): installing kills this process.
                    prefs.edit().putString("installing", v).commit();
                    h.post(() -> { status = "installing"; push(); });
                    s.commit(UpdateReceiver.statusSender(a)); // -> result(); success closes the app instead
                    id = -1;
                }
            } catch (IOException | NoSuchAlgorithmException | RuntimeException e) {
                if (id != -1) try { pi.abandonSession(id); } catch (RuntimeException ignored) { }
                if (!closed) h.post(() -> failed(v, "Download failed (" + e.getMessage() + ")"));
            }
        });
    }

    /** From UpdateReceiver: how the install went. Success never comes here: Android closes the app first. */
    static void result(int st, String msg) {
        Updater u = live;
        if (u != null) u.h.post(() -> u.onResult(st, msg));
    }

    void onResult(int st, String msg) {
        if (closed || latest == null) return;
        if (st == PackageInstaller.STATUS_SUCCESS) return;
        if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) { status = "confirm"; push(); return; } // Android's dialog is up
        failed(latest, st == PackageInstaller.STATUS_FAILURE_ABORTED ? "Install cancelled"
                // the usual reason: this copy and the release are signed with different keys
                : st == PackageInstaller.STATUS_FAILURE_CONFLICT ? latest + " can't replace this copy (different signing key): install it from the Releases page"
                : st == PackageInstaller.STATUS_FAILURE_STORAGE ? "Install failed: not enough storage"
                : "Install failed" + (msg == null ? "" : " (" + msg + ")"));
    }

    void failed(String v, String why) {
        prefs.edit().putString("failed", v).putString("failedWhy", why).remove("installing").apply();
        wantInstall = false;
        status = "available";
        error = why;
        push();
    }

    // ---- to the page --------------------------------------------------------------------------

    String info() {
        JSONObject o = new JSONObject();
        try {
            o.put("version", BuildConfig.VERSION_NAME).put("dev", dev).put("auto", auto())
                    .put("canInstall", canInstall()).put("askNotif", askNotif()).put("status", status).put("pct", pct)
                    .put("latest", latest == null ? JSONObject.NULL : latest)
                    .put("error", error == null ? JSONObject.NULL : error)
                    .put("checkedAt", prefs.getLong("checkedAt", 0));
        } catch (JSONException ignored) { }
        return o.toString();
    }

    void push() {
        if (!closed) a.js("window.onUpdate&&onUpdate(" + info() + ")");
    }

    /** Shown when the updated app couldn't reopen itself (UpdateReceiver). Runs in the new version. */
    static void notifyUpdated(Context c) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(CHANNEL, "App updates", NotificationManager.IMPORTANCE_DEFAULT);
        ch.setSound(null, null); // a desk display: no chime at night
        nm.createNotificationChannel(ch);
        PendingIntent open = PendingIntent.getActivity(c, 0, new Intent(c, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        nm.notify(NOTE_ID, new Notification.Builder(c, CHANNEL)
                .setSmallIcon(R.drawable.ic_launcher_foreground)
                .setContentTitle("Clawdmeter updated to " + BuildConfig.VERSION_NAME)
                .setContentText("Tap to open it again")
                .setContentIntent(open)
                .setAutoCancel(true)
                .build());
    }

    // ---- http ---------------------------------------------------------------------------------

    static HttpURLConnection open(String u) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(u).openConnection();
        c.setConnectTimeout(15_000);
        c.setReadTimeout(30_000);
        c.setRequestProperty("User-Agent", "Clawdmeter-Android/" + BuildConfig.VERSION_NAME);
        c.setRequestProperty("Accept", "application/vnd.github+json, application/octet-stream");
        int code = c.getResponseCode();
        if (code / 100 != 2) { c.disconnect(); throw new IOException("HTTP " + code); }
        return c;
    }

    static String get(String u) throws IOException {
        HttpURLConnection c = open(u);
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] buf = new byte[16 * 1024];
            for (int n; (n = in.read(buf)) > 0; ) b.write(buf, 0, n);
            return b.toString(StandardCharsets.UTF_8.name());
        } finally {
            c.disconnect();
        }
    }

    static String hex(byte[] b) {
        StringBuilder s = new StringBuilder();
        for (byte x : b) s.append(String.format("%02x", x));
        return s.toString();
    }
}
