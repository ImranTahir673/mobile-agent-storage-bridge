package com.agentstorage.copilot.data.local.entity

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "batches")
data class BatchEntity(
    @PrimaryKey
    @ColumnInfo(name = "batch_id")
    val batchId: String,

    @ColumnInfo(name = "created_at")
    val createdAt: String,

    @ColumnInfo(name = "intent")
    val intent: String,

    @ColumnInfo(name = "total_actions")
    val totalActions: Int,

    @ColumnInfo(name = "executed_actions")
    val executedActions: Int = 0,

    @ColumnInfo(name = "status")
    val status: String, // PENDING, RUNNING, COMPLETED, FAILED, ROLLED_BACK

    @ColumnInfo(name = "completed_at")
    val completedAt: String? = null,

    @ColumnInfo(name = "rolled_back_at")
    val rolledBackAt: String? = null
)
