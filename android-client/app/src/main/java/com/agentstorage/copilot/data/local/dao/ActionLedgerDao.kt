package com.agentstorage.copilot.data.local.dao

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Update
import com.agentstorage.copilot.data.local.entity.ActionLedgerEntity
import kotlinx.coroutines.flow.Flow

@Dao
interface ActionLedgerDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAction(action: ActionLedgerEntity): Long

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertActions(actions: List<ActionLedgerEntity>): List<Long>

    @Update
    suspend fun updateAction(action: ActionLedgerEntity)

    @Query("SELECT * FROM action_ledger WHERE batch_id = :batchId ORDER BY step_index ASC")
    suspend fun getActionsForBatch(batchId: String): List<ActionLedgerEntity>

    @Query("SELECT * FROM action_ledger WHERE batch_id = :batchId AND status = 'EXECUTED' ORDER BY step_index DESC")
    suspend fun getExecutedActionsForRollback(batchId: String): List<ActionLedgerEntity>

    @Query("SELECT * FROM action_ledger WHERE source_path LIKE '%' || :query || '%' OR destination_path LIKE '%' || :query || '%' ORDER BY id DESC")
    suspend fun lookupHistory(query: String): List<ActionLedgerEntity>

    @Query("SELECT * FROM action_ledger WHERE batch_id = :batchId ORDER BY step_index ASC")
    fun getActionsFlow(batchId: String): Flow<List<ActionLedgerEntity>>
}
