package com.agentstorage.copilot.data.local.entity

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
    tableName = "trash_index",
    foreignKeys = [
        ForeignKey(
            entity = ActionLedgerEntity::class,
            parentColumns = ["id"],
            childColumns = ["action_id"],
            onDelete = ForeignKey.CASCADE
        )
    ],
    indices = [
        Index("original_rel_path"),
        Index("action_id")
    ]
)
data class TrashIndexEntity(
    @PrimaryKey
    @ColumnInfo(name = "trash_id")
    val trashId: String,

    @ColumnInfo(name = "action_id")
    val actionId: Long,

    @ColumnInfo(name = "original_rel_path")
    val originalRelPath: String,

    @ColumnInfo(name = "trashed_rel_path")
    val trashedRelPath: String,

    @ColumnInfo(name = "file_size_bytes")
    val fileSizeBytes: Long,

    @ColumnInfo(name = "sha256_checksum")
    val sha256Checksum: String? = null,

    @ColumnInfo(name = "trashed_at")
    val trashedAt: String,

    @ColumnInfo(name = "expires_at")
    val expiresAt: String? = null,

    @ColumnInfo(name = "purged_at")
    val purgedAt: String? = null
)
