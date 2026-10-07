package com.agentstorage.copilot.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class ActionPlan(
    @SerialName("plan_id")
    val planId: String,
    @SerialName("version")
    val version: String = "1.0",
    @SerialName("timestamp")
    val timestamp: String = "",
    @SerialName("description")
    val description: String,
    @SerialName("dry_run")
    val dryRun: Boolean = false,
    @SerialName("collision_strategy")
    val collisionStrategy: String = "FAIL",
    @SerialName("actions")
    val actions: List<PlanAction> = emptyList()
)

@Serializable
data class PlanAction(
    @SerialName("action_id")
    val actionId: String,
    @SerialName("type")
    val type: String, // "make_dir", "move", "copy", "trash"
    @SerialName("path")
    val path: String? = null,
    @SerialName("source")
    val source: String? = null,
    @SerialName("destination")
    val destination: String? = null,
    @SerialName("expected_checksum")
    val expectedChecksum: String? = null
)

enum class CollisionStrategy {
    FAIL,
    RENAME_NUMERIC,
    RENAME_TIMESTAMP,
    SKIP
}

sealed class ValidationResult {
    data class Success(val plan: ActionPlan) : ValidationResult()
    data class Failure(val reason: String) : ValidationResult()
}
