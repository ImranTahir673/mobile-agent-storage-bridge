package com.agentstorage.copilot.service

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import com.agentstorage.copilot.CopilotApplication
import com.agentstorage.copilot.MainActivity
import com.agentstorage.copilot.R
import com.agentstorage.copilot.core.gatekeeper.PolicyGatekeeper
import com.agentstorage.copilot.core.storage.StorageManager
import com.agentstorage.copilot.core.storage.TrashVault
import com.agentstorage.copilot.data.local.AppDatabase
import com.agentstorage.copilot.data.local.entity.ActionLedgerEntity
import com.agentstorage.copilot.data.local.entity.BatchEntity
import com.agentstorage.copilot.data.local.entity.TrashIndexEntity
import com.agentstorage.copilot.data.model.ActionPlan
import com.agentstorage.copilot.data.model.CollisionStrategy
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

class ExecutionForegroundService : Service() {

    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var wakeLock: PowerManager.WakeLock? = null
    private lateinit var database: AppDatabase

    companion object {
        const val ACTION_EXECUTE_PLAN = "com.agentstorage.copilot.EXECUTE_PLAN"
        const val ACTION_ROLLBACK_BATCH = "com.agentstorage.copilot.ROLLBACK_BATCH"
        const val EXTRA_PLAN_JSON = "extra_plan_json"
        const val EXTRA_BATCH_ID = "extra_batch_id"
        private const val NOTIFICATION_ID = 1001

        fun startExecution(context: Context, planJson: String) {
            val intent = Intent(context, ExecutionForegroundService::class.java).apply {
                action = ACTION_EXECUTE_PLAN
                putExtra(EXTRA_PLAN_JSON, planJson)
            }
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun startRollback(context: Context, batchId: String) {
            val intent = Intent(context, ExecutionForegroundService::class.java).apply {
                action = ACTION_ROLLBACK_BATCH
                putExtra(EXTRA_BATCH_ID, batchId)
            }
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        database = (application as CopilotApplication).database
        acquireWakeLock()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildNotification("Initializing storage copilot..."))

        when (intent?.action) {
            ACTION_EXECUTE_PLAN -> {
                val planJson = intent.getStringExtra(EXTRA_PLAN_JSON)
                if (planJson != null) {
                    serviceScope.launch {
                        handlePlanExecution(planJson)
                    }
                } else {
                    stopSelf()
                }
            }
            ACTION_ROLLBACK_BATCH -> {
                val batchId = intent.getStringExtra(EXTRA_BATCH_ID)
                if (batchId != null) {
                    serviceScope.launch {
                        handleRollback(batchId)
                    }
                } else {
                    stopSelf()
                }
            }
            else -> stopSelf()
        }

        return START_NOT_STICKY
    }

    private suspend fun handlePlanExecution(planJson: String) {
        val baseDir = android.os.Environment.getExternalStorageDirectory()
        val gatekeeper = PolicyGatekeeper(baseDir)
        val trashVault = TrashVault(baseDir)
        val storageManager = StorageManager(baseDir, gatekeeper, trashVault)

        val plan = Json { ignoreUnknownKeys = true }.decodeFromString<ActionPlan>(planJson)
        val batchId = plan.planId
        val timestamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())

        // 1. Pre-log batch in database
        val batchEntity = BatchEntity(
            batchId = batchId,
            createdAt = timestamp,
            intent = plan.description,
            totalActions = plan.actions.size,
            status = "PENDING"
        )
        database.batchDao().insertBatch(batchEntity)

        // 2. Pre-log actions and calculate inverted undo vectors
        val preLoggedIds = mutableListOf<Long>()
        plan.actions.forEachIndexed { index, action ->
            val undo = calculateUndoVector(action)
            val actionEntity = ActionLedgerEntity(
                batchId = batchId,
                stepIndex = index + 1,
                actionType = action.type,
                sourcePath = action.source ?: action.path,
                destinationPath = action.destination ?: action.path,
                undoActionType = undo.first,
                undoSourcePath = undo.second,
                undoDestinationPath = undo.third,
                status = "PRE_LOGGED"
            )
            val id = database.actionLedgerDao().insertAction(actionEntity)
            preLoggedIds.add(id)
        }

        database.batchDao().updateBatch(batchEntity.copy(status = "RUNNING"))
        updateNotification("Executing batch: ${plan.actions.size} actions...")

        val strategy = try {
            CollisionStrategy.valueOf(plan.collisionStrategy)
        } catch (e: Exception) {
            CollisionStrategy.FAIL
        }

        var executedCount = 0
        var failed = false
        var errorMsg: String? = null

        // 3. Execute step-by-step with auto-rollback on failure
        for ((idx, action) in plan.actions.withIndex()) {
            val recordId = preLoggedIds[idx]
            try {
                when (action.type) {
                    "make_dir" -> {
                        action.path?.let { storageManager.makeDir(it) }
                    }
                    "move" -> {
                        val src = action.source ?: throw IllegalArgumentException("Missing source")
                        val dst = action.destination ?: throw IllegalArgumentException("Missing destination")
                        storageManager.moveFile(src, dst, strategy)
                    }
                    "copy" -> {
                        val src = action.source ?: throw IllegalArgumentException("Missing source")
                        val dst = action.destination ?: throw IllegalArgumentException("Missing destination")
                        storageManager.copyFile(src, dst, strategy)
                    }
                    "trash" -> {
                        val path = action.path ?: action.source ?: throw IllegalArgumentException("Missing path")
                        val trashedFile = storageManager.trashFile(path)
                        val trashId = UUID.randomUUID().toString()
                        val relTrash = trashedFile.canonicalPath.removePrefix(baseDir.canonicalPath).trimStart(File.separatorChar)

                        database.trashIndexDao().insertTrashEntry(
                            TrashIndexEntity(
                                trashId = trashId,
                                actionId = recordId,
                                originalRelPath = path,
                                trashedRelPath = relTrash,
                                fileSizeBytes = trashedFile.length(),
                                trashedAt = timestamp
                            )
                        )
                    }
                }

                // Update action ledger status
                val actionEntity = database.actionLedgerDao().getActionsForBatch(batchId)[idx]
                database.actionLedgerDao().updateAction(
                    actionEntity.copy(status = "EXECUTED", executedAt = timestamp)
                )
                executedCount++
            } catch (exc: Exception) {
                failed = true
                errorMsg = exc.message
                val actionEntity = database.actionLedgerDao().getActionsForBatch(batchId)[idx]
                database.actionLedgerDao().updateAction(
                    actionEntity.copy(status = "FAILED", errorMessage = exc.message)
                )
                break
            }
        }

        if (failed) {
            updateNotification("Error occurred. Rolling back executed steps...")
            handleRollback(batchId)
            database.batchDao().updateBatch(
                batchEntity.copy(
                    status = "FAILED",
                    executedActions = executedCount,
                    completedAt = timestamp
                )
            )
        } else {
            database.batchDao().updateBatch(
                batchEntity.copy(
                    status = "COMPLETED",
                    executedActions = executedCount,
                    completedAt = timestamp
                )
            )
            updateNotification("Completed: $executedCount actions executed.")
        }

        stopForeground(STOP_FOREGROUND_DETACH)
        stopSelf()
    }

    private suspend fun handleRollback(batchId: String) {
        val baseDir = android.os.Environment.getExternalStorageDirectory()
        val gatekeeper = PolicyGatekeeper(baseDir)
        val trashVault = TrashVault(baseDir)
        val storageManager = StorageManager(baseDir, gatekeeper, trashVault)

        // Query executed actions in reverse order
        val executedActions = database.actionLedgerDao().getExecutedActionsForRollback(batchId)
        val timestamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())

        for (action in executedActions) {
            try {
                when (action.undoActionType) {
                    "remove_dir" -> {
                        action.undoSourcePath?.let { storageManager.removeDirIfEmpty(it) }
                    }
                    "move" -> {
                        val src = action.undoSourcePath
                        val dst = action.undoDestinationPath
                        if (src != null && dst != null) {
                            storageManager.moveFile(src, dst, CollisionStrategy.FAIL)
                        }
                    }
                    "delete_copy" -> {
                        action.undoSourcePath?.let { storageManager.trashFile(it) }
                    }
                    "untrash" -> {
                        val originalPath = action.undoDestinationPath
                        if (originalPath != null) {
                            val trashEntry = database.trashIndexDao().getTrashEntryByActionId(action.id)
                            if (trashEntry != null) {
                                val trashedFile = gatekeeper.resolveSafePath(trashEntry.trashedRelPath)
                                val originalFile = gatekeeper.resolveSafePath(originalPath)
                                trashVault.restoreFile(trashedFile, originalFile)
                                database.trashIndexDao().updateTrashEntry(trashEntry.copy(purgedAt = timestamp))
                            }
                        }
                    }
                }
                database.actionLedgerDao().updateAction(
                    action.copy(status = "REVERTED", revertedAt = timestamp)
                )
            } catch (e: Exception) {
                // Log revert failure
            }
        }

        val batch = database.batchDao().getBatchById(batchId)
        if (batch != null) {
            database.batchDao().updateBatch(batch.copy(status = "ROLLED_BACK", rolledBackAt = timestamp))
        }
        updateNotification("Rollback completed for batch: $batchId")
        stopForeground(STOP_FOREGROUND_DETACH)
        stopSelf()
    }

    private fun calculateUndoVector(action: com.agentstorage.copilot.data.model.PlanAction): Triple<String, String?, String?> {
        return when (action.type) {
            "make_dir" -> Triple("remove_dir", action.path, null)
            "move" -> Triple("move", action.destination, action.source)
            "copy" -> Triple("delete_copy", action.destination, null)
            "trash" -> Triple("untrash", null, action.path ?: action.source)
            else -> Triple("none", null, null)
        }
    }

    private fun buildNotification(contentText: String): Notification {
        val launchIntent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CopilotApplication.CHANNEL_EXECUTION_ID)
            .setContentTitle("Agent Storage Copilot")
            .setContentText(contentText)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(contentText: String) {
        val manager = getSystemService(android.app.NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, buildNotification(contentText))
    }

    private fun acquireWakeLock() {
        val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = powerManager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Copilot:ExecutionWakeLock").apply {
            acquire(10 * 60 * 1000L) // 10 minutes max
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        serviceScope.cancel()
        if (wakeLock?.isHeld == true) {
            wakeLock?.release()
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
