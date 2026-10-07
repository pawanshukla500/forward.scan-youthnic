package shop.youthnic.scan.update

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters

/** Every few hours, even when the app is closed: a newer build on the server shows the "update available" notification. */
class UpdateCheckWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result {
        AppUpdater.check(applicationContext, force = true)  // notifies once per version by itself
        return Result.success()  // a failed check simply waits for the next run
    }
}
