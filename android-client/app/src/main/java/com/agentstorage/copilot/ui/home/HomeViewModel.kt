package com.agentstorage.copilot.ui.home

import android.app.Application
import android.content.Context
import android.os.Environment
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.agentstorage.copilot.BuildConfig
import com.agentstorage.copilot.CopilotApplication
import com.agentstorage.copilot.core.gatekeeper.PolicyGatekeeper
import com.agentstorage.copilot.core.privacy.PiiSanitizer
import com.agentstorage.copilot.core.storage.StorageManager
import com.agentstorage.copilot.core.storage.TrashVault
import com.agentstorage.copilot.data.local.entity.ActionLedgerEntity
import com.agentstorage.copilot.data.local.entity.BatchEntity
import com.agentstorage.copilot.data.local.entity.TrashIndexEntity
import com.agentstorage.copilot.data.model.ActionPlan
import com.agentstorage.copilot.data.model.CollisionStrategy
import com.agentstorage.copilot.data.model.PlanAction
import com.agentstorage.copilot.data.model.ValidationResult
import com.agentstorage.copilot.data.profile.UserProfileRepository
import com.agentstorage.copilot.data.remote.GeminiApiClient
import com.agentstorage.copilot.service.ExecutionForegroundService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

data class HomeUiState(
    val promptText: String = "",
    val isLoading: Boolean = false,
    val statusMessage: String = "Ready for instructions",
    val candidatePlan: ActionPlan? = null,
    val lastCompletedBatchId: String? = null,
    val errorMessage: String? = null,
    val hasApiKey: Boolean = false,
    val conversationalResponse: String? = null
)

class HomeViewModel(application: Application) : AndroidViewModel(application) {

    private val _uiState = MutableStateFlow(HomeUiState())
    val uiState: StateFlow<HomeUiState> = _uiState.asStateFlow()

    private val baseDir = Environment.getExternalStorageDirectory()
    private val gatekeeper = PolicyGatekeeper(baseDir)
    private val trashVault = TrashVault(baseDir)
    private val storageManager = StorageManager(baseDir, gatekeeper, trashVault)
    private val profileRepository = UserProfileRepository(application, baseDir)
    private val geminiClient = GeminiApiClient()
    private val database = (application as CopilotApplication).database

    init {
        val key = getSavedApiKey()
        val hasKey = key.isNotBlank()
        _uiState.value = _uiState.value.copy(
            hasApiKey = hasKey,
            errorMessage = if (!hasKey) "Gemini API key is not configured. Please configure your key in app settings." else null
        )

        viewModelScope.launch(Dispatchers.IO) {
            try {
                val latest = database.batchDao().getLatestCompletedBatch()
                if (latest != null) {
                    _uiState.value = _uiState.value.copy(lastCompletedBatchId = latest.batchId)
                }
            } catch (e: Exception) {
                // Database fallback
            }
        }
    }

    fun getSavedApiKey(): String {
        return try {
            val prefs = getApplication<Application>().getSharedPreferences("copilot_settings", Context.MODE_PRIVATE)
            val savedKey = prefs.getString("gemini_api_key", null)
            if (!savedKey.isNullOrBlank()) {
                savedKey
            } else {
                BuildConfig.DEFAULT_GEMINI_API_KEY
            }
        } catch (e: Exception) {
            BuildConfig.DEFAULT_GEMINI_API_KEY
        }
    }

    fun saveApiKey(key: String) {
        try {
            val prefs = getApplication<Application>().getSharedPreferences("copilot_settings", Context.MODE_PRIVATE)
            prefs.edit().putString("gemini_api_key", key).apply()
        } catch (e: Exception) {
            // Graceful fallback
        }
        val isConfigured = key.isNotBlank() || BuildConfig.DEFAULT_GEMINI_API_KEY.isNotBlank()
        _uiState.value = _uiState.value.copy(
            hasApiKey = isConfigured,
            errorMessage = if (isConfigured) null else _uiState.value.errorMessage
        )
    }

    fun updateApiKeyStatus(hasKey: Boolean) {
        _uiState.value = _uiState.value.copy(
            hasApiKey = hasKey,
            errorMessage = if (hasKey) null else _uiState.value.errorMessage
        )
    }

    fun onPromptChange(newText: String) {
        _uiState.value = _uiState.value.copy(promptText = newText)
    }

    fun clearError() {
        _uiState.value = _uiState.value.copy(errorMessage = null)
    }

    fun dismissConversationalResponse() {
        _uiState.value = _uiState.value.copy(conversationalResponse = null)
    }

    /**
     * Dual-Mode Intent Classifier:
     * Differentiates conversational inquiries (greetings, FAQs) from explicit file mutation operations.
     */
    fun isConversationalQuery(query: String): Boolean {
        val trimmed = query.trim().lowercase()
        val conversationalPatterns = listOf(
            "^hi\\b.*", "^hello\\b.*", "^hey\\b.*", "^greetings\\b.*",
            ".*what can you do.*", ".*how does this work.*", ".*who are you.*",
            ".*help\\b.*", ".*capabilities.*", ".*what are you.*", "^explain.*"
        )
        if (conversationalPatterns.any { trimmed.matches(it.toRegex()) }) {
            return true
        }

        val mutationKeywords = listOf(
            "organize", "sort", "move", "rename", "clean", "cleanup", "gather",
            "trash", "delete", "remove", "categorize", "separate", "archive", "group", "collect"
        )
        return mutationKeywords.none { trimmed.contains(it) }
    }

    fun generatePlan(apiKey: String = "", customGoal: String? = null) {
        val resolvedKey = if (apiKey.isNotBlank()) apiKey else getSavedApiKey()
        val goal = customGoal ?: _uiState.value.promptText
        if (goal.isBlank()) return

        if (resolvedKey.isBlank()) {
            _uiState.value = _uiState.value.copy(
                hasApiKey = false,
                errorMessage = "Gemini API key is not configured. Please configure your key in app settings."
            )
            return
        }

        // Mode 1: Pure Conversational / Inquiry Intent
        if (isConversationalQuery(goal)) {
            _uiState.value = _uiState.value.copy(
                hasApiKey = true,
                isLoading = true,
                statusMessage = "Thinking...",
                errorMessage = null,
                candidatePlan = null,
                conversationalResponse = null
            )

            viewModelScope.launch(Dispatchers.IO) {
                try {
                    val systemPrompt = """
                        You are Agent Storage Copilot, a privacy-first, on-device semantic file manager for Android.
                        The user asked a greeting, question, or general capability inquiry.
                        Respond warmly, concisely, and professionally in clean Markdown:
                        1. Explain your capabilities on Android:
                           - 📂 **Organize Downloads**: Categorize chaotic download folders by category or extension
                           - 📋 **Sort Student Vouchers**: Gather university fee vouchers and challans into Documents/University/Vouchers
                           - 👥 **Peer Separation**: Detect and separate peer documents (Fawad, Sumbal, Ahmed, Yousaf) into Documents/Peers/<Name>
                           - 🏷️ **Clean File Names**: Rename messy downloads based on extracted text snippets
                           - 🛡️ **Zero Cloud Storage & 100% Reversible**: Operations are logged in a local SQLite ledger (.ledger.db) with 1-tap Undo
                        2. Suggest 3–4 concrete prompt ideas the user can tap or type to start.
                    """.trimIndent()

                    val response = geminiClient.generateConversationalResponse(
                        apiKey = resolvedKey,
                        systemPrompt = systemPrompt,
                        userPrompt = goal
                    )

                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        statusMessage = "Ready for instructions",
                        conversationalResponse = response.trim(),
                        candidatePlan = null
                    )
                } catch (e: Exception) {
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        errorMessage = e.message ?: "Failed to generate response"
                    )
                }
            }
            return
        }

        // Mode 2: File Mutation Intent (Stage 1 Reconnaissance -> Stage 2 Plan Synthesis)
        _uiState.value = _uiState.value.copy(
            hasApiKey = true,
            isLoading = true,
            statusMessage = "Performing local reconnaissance...",
            errorMessage = null,
            conversationalResponse = null
        )

        viewModelScope.launch(Dispatchers.IO) {
            try {
                val profile = try {
                    profileRepository.getProfile()
                } catch (e: Exception) {
                    com.agentstorage.copilot.data.model.UserProfile()
                }

                // Stage 1: Reconnaissance (Local Inspection & Tier 1 PII Scrub)
                val candidateFiles = try {
                    val downloadFiles = storageManager.listFiles("Download")
                    if (downloadFiles.isNotEmpty()) downloadFiles else storageManager.listFiles("Documents")
                } catch (e: Exception) {
                    emptyList()
                }

                val reconnaissanceSummary = buildString {
                    appendLine("Target Folder: Download")
                    appendLine("Total Files Detected: ${candidateFiles.size}")
                    appendLine("Inspected Candidate Files:")
                    candidateFiles.take(20).forEach { f ->
                        val snippet = if (!f.isDirectory) {
                            storageManager.readFileSnippet(f.relativePath, 500)
                        } else ""
                        appendLine("- ${f.name} (Size: ${f.sizeBytes} bytes, RelativePath: ${f.relativePath})")
                        if (snippet.isNotBlank()) {
                            appendLine("  Snippet: $snippet")
                        }
                    }
                }

                _uiState.value = _uiState.value.copy(statusMessage = "Reasoning with Gemini...")

                // Stage 2: Plan Synthesis (Enforcing Blast Radius, Peer Separation & Policy Rules)
                val systemPrompt = """
                    You are an Android Local Storage Copilot.
                    Analyze the reconnaissance summary and user goal. Return strictly valid JSON conforming to:
                    {
                      "plan_id": "<uuid>",
                      "description": "<summary of changes>",
                      "collision_strategy": "RENAME_NUMERIC",
                      "actions": [
                        { "action_id": "step-1", "type": "make_dir|move|copy|trash", "source": "...", "destination": "...", "path": "..." }
                      ]
                    }
                    Strict Policy Rules:
                    1. Limit to max 20 actions (blast radius cap).
                    2. Never delete files permanently; use 'trash'.
                    3. User Owner: ${profile.userIdentity.primaryName}.
                    4. Known Peers: ${profile.knownPeers.map { it.name }}.
                    5. Peer Separation Rule: Route files belonging to peers into ${profile.routingRules.peerDocumentsBase}/<PeerName>. NEVER move peer documents into ${profile.routingRules.personalDocumentsBase}.
                    6. Collision Strategy: Use 'RENAME_NUMERIC'.
                    If the user request cannot be translated into file actions, return helpful markdown instead of empty JSON.
                """.trimIndent()

                val rawResponse = geminiClient.generateActionPlan(
                    apiKey = resolvedKey,
                    systemPrompt = systemPrompt,
                    userPrompt = "Goal: $goal\n\nReconnaissance:\n$reconnaissanceSummary"
                )

                val cleanedJson = extractJson(rawResponse)
                if (cleanedJson.isNotBlank() && cleanedJson.contains("plan_id")) {
                    val parsedPlan = Json { ignoreUnknownKeys = true }.decodeFromString<ActionPlan>(cleanedJson)

                    // Gatekeeper Validation
                    when (val validation = gatekeeper.validatePlan(parsedPlan, profile)) {
                        is ValidationResult.Success -> {
                            _uiState.value = _uiState.value.copy(
                                isLoading = false,
                                statusMessage = "Plan generated (${validation.plan.actions.size} actions). Awaiting approval.",
                                candidatePlan = validation.plan,
                                conversationalResponse = null
                            )
                        }
                        is ValidationResult.Failure -> {
                            _uiState.value = _uiState.value.copy(
                                isLoading = false,
                                errorMessage = "Policy violation: ${validation.reason}"
                            )
                        }
                    }
                } else {
                    // Conversational fallback
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        statusMessage = "Ready for instructions",
                        conversationalResponse = rawResponse.trim(),
                        candidatePlan = null
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    errorMessage = e.message ?: "Failed to generate plan"
                )
            }
        }
    }

    /**
     * Executes approved action plan strictly on Dispatchers.IO.
     * Guaranteed zero Room DB or Disk I/O on Dispatchers.Main.
     */
    fun executeApprovedPlan(plan: ActionPlan) {
        _uiState.value = _uiState.value.copy(
            candidatePlan = null,
            isLoading = true,
            statusMessage = "Pre-logging execution plan...",
            errorMessage = null
        )

        viewModelScope.launch(Dispatchers.IO) {
            val batchId = plan.planId
            val timestamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())

            try {
                // 1. Pre-log batch in Room database
                val batchEntity = BatchEntity(
                    batchId = batchId,
                    createdAt = timestamp,
                    intent = plan.description,
                    totalActions = plan.actions.size,
                    status = "PENDING"
                )
                database.batchDao().insertBatch(batchEntity)

                // 2. Pre-log actions with inverted undo vectors
                val actionEntities = plan.actions.mapIndexed { index, action ->
                    val undo = calculateUndoVector(action)
                    ActionLedgerEntity(
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
                }
                val preLoggedIds = database.actionLedgerDao().insertActions(actionEntities)
                database.batchDao().updateBatch(batchEntity.copy(status = "RUNNING"))

                val strategy = try {
                    CollisionStrategy.valueOf(plan.collisionStrategy)
                } catch (e: Exception) {
                    CollisionStrategy.RENAME_NUMERIC
                }

                var executedCount = 0
                var failed = false
                var errorMsg: String? = null

                // 3. Step-by-step file execution
                for ((idx, action) in plan.actions.withIndex()) {
                    val recordId = preLoggedIds[idx]
                    val entity = actionEntities[idx]
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

                        // Update ledger record status
                        database.actionLedgerDao().updateAction(
                            entity.copy(id = recordId, status = "EXECUTED", executedAt = timestamp)
                        )
                        executedCount++
                    } catch (e: Exception) {
                        failed = true
                        errorMsg = e.message
                        database.actionLedgerDao().updateAction(
                            entity.copy(id = recordId, status = "FAILED", errorMessage = e.message)
                        )
                        break
                    }
                }

                if (failed) {
                    performRollbackInternal(batchId)
                    database.batchDao().updateBatch(
                        batchEntity.copy(
                            status = "FAILED",
                            executedActions = executedCount,
                            completedAt = timestamp
                        )
                    )
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        lastCompletedBatchId = null,
                        errorMessage = "Execution failed: ${errorMsg ?: "Unknown error"}. Changes rolled back."
                    )
                } else {
                    database.batchDao().updateBatch(
                        batchEntity.copy(
                            status = "COMPLETED",
                            executedActions = executedCount,
                            completedAt = timestamp
                        )
                    )
                    _uiState.value = _uiState.value.copy(
                        isLoading = false,
                        lastCompletedBatchId = batchId,
                        statusMessage = "Batch completed ($executedCount actions). Tap UNDO to revert byte-for-byte."
                    )
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    errorMessage = "Plan execution error: ${e.message}"
                )
            }
        }
    }

    fun dismissPlan() {
        _uiState.value = _uiState.value.copy(candidatePlan = null)
    }

    fun triggerRollback() {
        val batchId = _uiState.value.lastCompletedBatchId ?: return
        _uiState.value = _uiState.value.copy(isLoading = true, statusMessage = "Rolling back batch...")

        viewModelScope.launch(Dispatchers.IO) {
            try {
                performRollbackInternal(batchId)
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    lastCompletedBatchId = null,
                    statusMessage = "Batch successfully rolled back."
                )
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    errorMessage = "Rollback failed: ${e.message}"
                )
            }
        }
    }

    private suspend fun performRollbackInternal(batchId: String) {
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
    }

    private fun calculateUndoVector(action: PlanAction): Triple<String, String?, String?> {
        return when (action.type) {
            "make_dir" -> Triple("remove_dir", action.path, null)
            "move" -> Triple("move", action.destination, action.source)
            "copy" -> Triple("delete_copy", action.destination, null)
            "trash" -> Triple("untrash", null, action.path ?: action.source)
            else -> Triple("none", null, null)
        }
    }

    private fun extractJson(text: String): String {
        val start = text.indexOf('{')
        val end = text.lastIndexOf('}')
        if (start != -1 && end != -1 && end > start) {
            return text.substring(start, end + 1)
        }
        return ""
    }
}
