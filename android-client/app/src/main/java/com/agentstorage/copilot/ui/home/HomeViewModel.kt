package com.agentstorage.copilot.ui.home

import android.app.Application
import android.os.Environment
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.agentstorage.copilot.CopilotApplication
import com.agentstorage.copilot.core.gatekeeper.PolicyGatekeeper
import com.agentstorage.copilot.core.storage.StorageManager
import com.agentstorage.copilot.core.storage.TrashVault
import com.agentstorage.copilot.data.model.ActionPlan
import com.agentstorage.copilot.data.model.ValidationResult
import com.agentstorage.copilot.data.profile.UserProfileRepository
import com.agentstorage.copilot.data.remote.GeminiApiClient
import com.agentstorage.copilot.service.ExecutionForegroundService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File
import java.util.UUID

data class HomeUiState(
    val promptText: String = "",
    val isLoading: Boolean = false,
    val statusMessage: String = "Ready for instructions",
    val candidatePlan: ActionPlan? = null,
    val lastCompletedBatchId: String? = null,
    val errorMessage: String? = null
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
        viewModelScope.launch(Dispatchers.IO) {
            try {
                val latest = database.batchDao().getLatestCompletedBatch()
                if (latest != null) {
                    _uiState.value = _uiState.value.copy(lastCompletedBatchId = latest.batchId)
                }
            } catch (e: Exception) {
                // Graceful fallback if database has not been initialized yet
            }
        }
    }

    fun onPromptChange(newText: String) {
        _uiState.value = _uiState.value.copy(promptText = newText)
    }

    fun generatePlan(apiKey: String, customGoal: String? = null) {
        val goal = customGoal ?: _uiState.value.promptText
        if (goal.isBlank()) return

        if (apiKey.isBlank()) {
            _uiState.value = _uiState.value.copy(
                errorMessage = "Gemini API key is not configured. Please configure your key in app settings."
            )
            return
        }

        _uiState.value = _uiState.value.copy(
            isLoading = true,
            statusMessage = "Performing local reconnaissance...",
            errorMessage = null
        )

        viewModelScope.launch(Dispatchers.IO) {
            try {
                val profile = try {
                    profileRepository.getProfile()
                } catch (e: Exception) {
                    com.agentstorage.copilot.data.model.UserProfile()
                }

                val files = try {
                    storageManager.listFiles("Download")
                } catch (e: Exception) {
                    emptyList()
                }

                val reconnaissanceSummary = buildString {
                    appendLine("Target Folder: Download")
                    appendLine("Files found (${files.size}):")
                    files.take(15).forEach { f ->
                        val snippet = if (!f.isDirectory) {
                            storageManager.readFileSnippet(f.relativePath, 300)
                        } else ""
                        appendLine("- ${f.name} (Size: ${f.sizeBytes} bytes)")
                        if (snippet.isNotBlank()) {
                            appendLine("  Snippet: $snippet")
                        }
                    }
                }

                _uiState.value = _uiState.value.copy(statusMessage = "Reasoning with Gemini...")

                val systemPrompt = """
                    You are an Android Local Storage Copilot. Return strictly valid JSON conforming to:
                    {
                      "plan_id": "<uuid>",
                      "description": "<string>",
                      "collision_strategy": "RENAME_NUMERIC",
                      "actions": [
                        { "action_id": "step-1", "type": "make_dir|move|copy|trash", "source": "...", "destination": "...", "path": "..." }
                      ]
                    }
                    Limit to max 10 actions. Never delete files permanently; use 'trash'.
                    User Owner: ${profile.userIdentity.primaryName}.
                    Known Peers: ${profile.knownPeers.map { it.name }}. Route peer files to ${profile.routingRules.peerDocumentsBase}/<PeerName>.
                """.trimIndent()

                val rawResponse = geminiClient.generateActionPlan(
                    apiKey = apiKey,
                    systemPrompt = systemPrompt,
                    userPrompt = "Goal: $goal\n\nReconnaissance:\n$reconnaissanceSummary"
                )

                val cleanedJson = extractJson(rawResponse)
                val parsedPlan = Json { ignoreUnknownKeys = true }.decodeFromString<ActionPlan>(cleanedJson)

                // Gatekeeper Validation
                when (val validation = gatekeeper.validatePlan(parsedPlan, profile)) {
                    is ValidationResult.Success -> {
                        _uiState.value = _uiState.value.copy(
                            isLoading = false,
                            statusMessage = "Plan generated. Awaiting approval.",
                            candidatePlan = validation.plan
                        )
                    }
                    is ValidationResult.Failure -> {
                        _uiState.value = _uiState.value.copy(
                            isLoading = false,
                            errorMessage = "Policy violation: ${validation.reason}"
                        )
                    }
                }
            } catch (e: Exception) {
                _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    errorMessage = e.message ?: "Failed to generate plan"
                )
            }
        }
    }

    fun executeApprovedPlan(plan: ActionPlan) {
        _uiState.value = _uiState.value.copy(candidatePlan = null, isLoading = true, statusMessage = "Executing batch...")
        val planJson = Json.encodeToString(plan)
        ExecutionForegroundService.startExecution(getApplication(), planJson)
        _uiState.value = _uiState.value.copy(
            isLoading = false,
            statusMessage = "Execution started in background service.",
            lastCompletedBatchId = plan.planId
        )
    }

    fun dismissPlan() {
        _uiState.value = _uiState.value.copy(candidatePlan = null)
    }

    fun triggerRollback() {
        val batchId = _uiState.value.lastCompletedBatchId ?: return
        _uiState.value = _uiState.value.copy(isLoading = true, statusMessage = "Rolling back batch...")
        ExecutionForegroundService.startRollback(getApplication(), batchId)
        _uiState.value = _uiState.value.copy(
            isLoading = false,
            lastCompletedBatchId = null,
            statusMessage = "Rollback initiated."
        )
    }

    private fun extractJson(text: String): String {
        val start = text.indexOf('{')
        val end = text.lastIndexOf('}')
        if (start != -1 && end != -1 && end > start) {
            return text.substring(start, end + 1)
        }
        return text
    }
}
