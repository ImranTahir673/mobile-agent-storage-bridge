package com.agentstorage.copilot.core.storage

import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

class TrashVault(private val baseDir: File) {

    private val trashDir: File = File(baseDir, ".agent_trash")

    /**
     * Lazily ensures the .agent_trash directory exists on disk only when required.
     */
    private fun ensureTrashDirectory(): File {
        if (!trashDir.exists()) {
            try {
                trashDir.mkdirs()
            } catch (e: Exception) {
                // Defer filesystem errors to point of file creation
            }
        }
        return trashDir
    }

    /**
     * Soft-deletes a file into .agent_trash with timestamp and UUID prefix.
     * Preserves file metadata and returns relative path within storage root.
     */
    fun trashFile(sourceFile: File): File {
        if (!sourceFile.exists()) {
            throw NoSuchFileException(sourceFile, reason = "Source file does not exist.")
        }

        val dir = ensureTrashDirectory()
        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val shortUuid = UUID.randomUUID().toString().substring(0, 6)
        val trashFileName = "${timestamp}_${shortUuid}_${sourceFile.name}"
        val destinationFile = File(dir, trashFileName)

        if (!sourceFile.renameTo(destinationFile)) {
            // Fallback to copy & delete
            sourceFile.copyTo(destinationFile, overwrite = false)
            if (!sourceFile.delete()) {
                destinationFile.delete()
                throw java.io.IOException("Failed to soft-delete file: ${sourceFile.absolutePath}")
            }
        }
        return destinationFile
    }

    /**
     * Restores a previously trashed file back to its original location.
     */
    fun restoreFile(trashedFile: File, originalTargetFile: File) {
        if (!trashedFile.exists()) {
            throw NoSuchFileException(trashedFile, reason = "Trashed file no longer exists in vault.")
        }
        val parent = originalTargetFile.parentFile
        if (parent != null && !parent.exists()) {
            parent.mkdirs()
        }

        if (!trashedFile.renameTo(originalTargetFile)) {
            trashedFile.copyTo(originalTargetFile, overwrite = false)
            trashedFile.delete()
        }
    }

    fun getTrashDirectory(): File = ensureTrashDirectory()
}
