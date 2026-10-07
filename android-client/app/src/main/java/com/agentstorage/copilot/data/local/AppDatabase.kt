package com.agentstorage.copilot.data.local

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.sqlite.db.SupportSQLiteDatabase
import com.agentstorage.copilot.data.local.dao.ActionLedgerDao
import com.agentstorage.copilot.data.local.dao.BatchDao
import com.agentstorage.copilot.data.local.dao.TrashIndexDao
import com.agentstorage.copilot.data.local.entity.ActionLedgerEntity
import com.agentstorage.copilot.data.local.entity.BatchEntity
import com.agentstorage.copilot.data.local.entity.TrashIndexEntity

@Database(
    entities = [
        BatchEntity::class,
        ActionLedgerEntity::class,
        TrashIndexEntity::class
    ],
    version = 1,
    exportSchema = false
)
abstract class AppDatabase : RoomDatabase() {

    abstract fun batchDao(): BatchDao
    abstract fun actionLedgerDao(): ActionLedgerDao
    abstract fun trashIndexDao(): TrashIndexDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        fun getDatabase(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "agent_copilot_ledger.db"
                )
                    .addCallback(object : Callback() {
                        override fun onOpen(db: SupportSQLiteDatabase) {
                            super.onOpen(db)
                            // Enable Write-Ahead Logging (WAL) and foreign keys
                            db.execSQL("PRAGMA journal_mode = WAL;")
                            db.execSQL("PRAGMA foreign_keys = ON;")
                        }
                    })
                    .fallbackToDestructiveMigration()
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
