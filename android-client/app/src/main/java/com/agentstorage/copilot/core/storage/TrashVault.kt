package com.agentstorage.copilot.core.storage

import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

class TrashVault(private val baseDir: File) {

    private val trashDir: File = File(baseDir, ".agent_trash")

    init {
        if (!trashDir.exists()) {
            trashDir.mkdirs()
        }
    }

    /**
     * Soft-deletes a file into .agent_trash with timestamp and UUID prefix.
     * Preserves file metadata and returns relative path within storage root.
     */
    fun trashFile(sourceFile: File): File {
        if (!sourceFile.exists()) {
            throw NoSuchFileException(sourceFile, reason = "Source file does not exist.")
        }

        val timestamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())
        val shortUuid = UUID.randomUUID().toString().substring(0, 6)
        val trashFileName = "${timestamp}_${shortUuid}_${sourceFile.name}"
        val destinationFile = File(trashDir, trashFileName)

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

    fun getTrashDirectory(): File = trashDir
}
