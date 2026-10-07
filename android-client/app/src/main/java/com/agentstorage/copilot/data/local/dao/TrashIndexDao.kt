package com.agentstorage.copilot.data.local.dao

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Update
import com.agentstorage.copilot.data.local.entity.TrashIndexEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface TrashIndexDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertTrashEntry(entry: TrashIndexEntity)

    @Update
    suspend fun updateTrashEntry(entry: TrashIndexEntity)

    @Query("SELECT * FROM trash_index WHERE original_rel_path = :originalRelPath AND purged_at IS NULL LIMIT 1")
    suspend fun getActiveTrashEntry(originalRelPath: String): TrashIndexEntity?

    @Query("SELECT * FROM trash_index WHERE action_id = :actionId LIMIT 1")
    suspend fun getTrashEntryByActionId(actionId: Long): TrashIndexEntity?

    @Query("SELECT * FROM trash_index WHERE purged_at IS NULL ORDER BY trashed_at DESC")
    fun getActiveTrashFlow(): Flow<List<TrashIndexEntity>>

    @Query("SELECT * FROM trash_index WHERE original_rel_path LIKE '%' || :query || '%' OR trashed_rel_path LIKE '%' || :query || '%'")
    suspend fun lookupTrash(query: String): List<TrashIndexEntity>
}
