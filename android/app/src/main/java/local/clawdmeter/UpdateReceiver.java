package local.clawdmeter;

import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentSender;
import android.content.SharedPreferences;
import android.content.pm.PackageInstaller;

/** Results of the installs Updater starts, and the relaunch after this app has replaced itself. */
public class UpdateReceiver extends BroadcastReceiver {
    static final String RESULT = "local.clawdmeter.INSTALL_RESULT";

    static IntentSender statusSender(Context c) {
        Intent i = new Intent(c, UpdateReceiver.class).setAction(RESULT);
        // mutable: the installer adds the status extras
        return PendingIntent.getBroadcast(c, 0, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE).getIntentSender();
    }

    @Override
    public void onReceive(Context c, Intent i) {
        if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(i.getAction())) { reopen(c); return; }
        if (!RESULT.equals(i.getAction())) return;
        int st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE);
        if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) { // Android wants a confirmation after all: show its dialog
            Intent confirm = i.getParcelableExtra(Intent.EXTRA_INTENT, Intent.class);
            if (confirm != null) {
                try { c.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); } catch (RuntimeException ignored) { }
            }
        }
        Updater.result(st, i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE));
    }

    /**
     * Android closed this app to replace it. As the Home app (kiosk mode) Android reopens it; otherwise Android
     * usually blocks a background app from opening itself, so also leave a notification to tap (the activity
     * removes it when it does open).
     */
    static void reopen(Context c) {
        SharedPreferences p = c.getSharedPreferences(Updater.PREFS, Context.MODE_PRIVATE);
        if (p.getString("installing", null) == null) return; // adb or a file manager did it, or the app is already up
        p.edit().remove("installing").apply();
        Updater.notifyUpdated(c);
        try {
            c.startActivity(new Intent(c, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (RuntimeException ignored) { }
    }
}
