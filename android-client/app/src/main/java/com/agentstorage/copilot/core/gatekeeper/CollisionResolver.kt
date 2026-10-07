package com.agentstorage.copilot.core.gatekeeper

import com.agentstorage.copilot.data.model.CollisionStrategy
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

object CollisionResolver {

    /**
     * Resolves a destination collision according to the selected strategy.
     * Returns the resolved File path, or null if the action should be skipped.
     * Throws an IllegalStateException if the strategy is FAIL and the file exists.
     */
    fun resolve(destinationFile: File, strategy: CollisionStrategy): File? {
        if (!destinationFile.exists()) {
            return destinationFile
        }

        return when (strategy) {
            CollisionStrategy.FAIL -> {
                throw IllegalStateException("Destination collision detected at: ${destinationFile.absolutePath}")
            }
            CollisionStrategy.RENAME_NUMERIC -> {
                findNumericCandidate(destinationFile)
            }
            CollisionStrategy.RENAME_TIMESTAMP -> {
                val timestamp = SimpleDateFormat("yyyyMMdd_HHmmssSSS", Locale.US).format(Date())
                val parent = destinationFile.parentFile
                val name = destinationFile.nameWithoutExtension
                val ext = destinationFile.extension
                val newName = if (ext.isNotEmpty()) "${name}_$timestamp.$ext" else "${name}_$timestamp"
                File(parent, newName)
            }
            CollisionStrategy.SKIP -> null
        }
    }

    private fun findNumericCandidate(target: File): File {
        val parent = target.parentFile
        val baseName = target.nameWithoutExtension
        val ext = target.extension

        var counter = 1
        while (counter < 1000) {
            val candidateName = if (ext.isNotEmpty()) "$baseName ($counter).$ext" else "$baseName ($counter)"
            val candidate = File(parent, candidateName)
            if (!candidate.exists()) {
                return candidate
            }
            counter++
        }
        return File(parent, "${baseName}_${System.currentTimeMillis()}.$ext")
    }
}
