package com.agentstorage.copilot.data.local.entity

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
    tableName = "action_ledger",
    foreignKeys = [
        ForeignKey(
            entity = BatchEntity::class,
            parentColumns = ["batch_id"],
            childColumns = ["batch_id"],
            onDelete = ForeignKey.CASCADE
        )
    ],
    indices = [
        Index("batch_id"),
        Index("status")
    ]
)
data class ActionLedgerEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,

    @ColumnInfo(name = "batch_id")
    val batchId: String,

    @ColumnInfo(name = "step_index")
    val stepIndex: Int,

    @ColumnInfo(name = "action_type")
    val actionType: String, // make_dir, move, copy, trash

    @ColumnInfo(name = "source_path")
    val sourcePath: String?,

    @ColumnInfo(name = "destination_path")
    val destinationPath: String?,

    @ColumnInfo(name = "file_size_bytes")
    val fileSizeBytes: Long = 0L,

    @ColumnInfo(name = "sha256_checksum")
    val sha256Checksum: String? = null,

    // Inverted Undo Vector definition
    @ColumnInfo(name = "undo_action_type")
    val undoActionType: String, // remove_dir, move, delete_copy, untrash

    @ColumnInfo(name = "undo_source_path")
    val undoSourcePath: String?,

    @ColumnInfo(name = "undo_destination_path")
    val undoDestinationPath: String?,

    @ColumnInfo(name = "status")
    val status: String, // PRE_LOGGED, EXECUTED, FAILED, REVERTED

    @ColumnInfo(name = "executed_at")
    val executedAt: String? = null,

    @ColumnInfo(name = "reverted_at")
    val revertedAt: String? = null,

    @ColumnInfo(name = "error_message")
    val errorMessage: String? = null
)
