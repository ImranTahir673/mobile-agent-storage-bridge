package com.agentstorage.copilot.core.storage

import com.agentstorage.copilot.core.gatekeeper.CollisionResolver
import com.agentstorage.copilot.core.gatekeeper.PolicyGatekeeper
import com.agentstorage.copilot.core.privacy.PiiSanitizer
import com.agentstorage.copilot.data.model.CollisionStrategy
import java.io.File
import java.io.FileInputStream
import java.security.MessageDigest

data class StorageFileInfo(
    val name: String,
    val relativePath: String,
    val isDirectory: Boolean,
    val sizeBytes: Long,
    val lastModified: Long
)

class StorageManager(
    val baseDir: File,
    val gatekeeper: PolicyGatekeeper,
    val trashVault: TrashVault
) {

    /**
     * Lists files relative to base storage root, sorted by newest first.
     */
    fun listFiles(relativePath: String = ""): List<StorageFileInfo> {
        val target = gatekeeper.resolveSafePath(relativePath)
        if (!target.exists() || !target.isDirectory) return emptyList()

        val files = target.listFiles()?.sortedByDescending { it.lastModified() } ?: return emptyList()
        val results = mutableListOf<StorageFileInfo>()

        for (file in files) {
            if (gatekeeper.isBlacklisted(file) || file.name.startsWith(".")) continue

            val relPath = file.canonicalPath.removePrefix(baseDir.canonicalPath).trimStart(File.separatorChar)
            results.add(
                StorageFileInfo(
                    name = file.name,
                    relativePath = relPath.replace('\\', '/'),
                    isDirectory = file.isDirectory,
                    sizeBytes = if (file.isFile) file.length() else 0L,
                    lastModified = file.lastModified()
                )
            )
        }
        return results
    }

    /**
     * Reads a preview snippet of a file, scrubbing sensitive Tier 1 PII before returning.
     * Extracts readable text strings safely from documents, text, and binary formats.
     */
    fun readFileSnippet(relativePath: String, maxChars: Int = 1000): String {
        return try {
            val file = gatekeeper.resolveSafePath(relativePath)
            if (!file.exists() || !file.isFile || file.length() == 0L) return ""

            val ext = file.extension.lowercase()
            val rawSnippet = when (ext) {
                "txt", "csv", "json", "xml", "md", "log", "html", "ini", "conf" -> {
                    file.bufferedReader(Charsets.UTF_8).use { reader ->
                        val buffer = CharArray(maxChars)
                        val read = reader.read(buffer, 0, maxChars)
                        if (read > 0) String(buffer, 0, read) else ""
                    }
                }
                "pdf" -> {
                    // Extract printable ASCII text chunks from PDF stream
                    val bytesToRead = minOf(file.length().toInt(), 8192)
                    val buffer = ByteArray(bytesToRead)
                    file.inputStream().use { it.read(buffer) }
                    extractPrintableText(buffer, maxChars)
                }
                else -> {
                    // Default safe printable extraction
                    val bytesToRead = minOf(file.length().toInt(), 4096)
                    val buffer = ByteArray(bytesToRead)
                    file.inputStream().use { it.read(buffer) }
                    extractPrintableText(buffer, maxChars)
                }
            }

            if (rawSnippet.isBlank()) "" else PiiSanitizer.sanitizeSnippet(rawSnippet)
        } catch (e: Exception) {
            ""
        }
    }

    private fun extractPrintableText(bytes: ByteArray, maxChars: Int): String {
        val sb = StringBuilder()
        for (b in bytes) {
            val c = b.toInt().toChar()
            if (c in ' '..'~' || c == '\n' || c == '\r' || c == '\t') {
                sb.append(c)
                if (sb.length >= maxChars) break
            } else if (sb.isNotEmpty() && sb.last() != ' ') {
                sb.append(' ')
            }
        }
        return sb.toString().trim()
    }

    /**
     * Calculates SHA256 checksum for a file.
     */
    fun calculateChecksum(file: File): String {
        if (!file.exists() || !file.isFile) return ""
        val digest = MessageDigest.getInstance("SHA-256")
        FileInputStream(file).use { fis ->
            val buffer = ByteArray(8192)
            var bytesRead: Int
            while (fis.read(buffer).also { bytesRead = it } != -1) {
                digest.update(buffer, 0, bytesRead)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    fun makeDir(relativePath: String): File {
        val target = gatekeeper.resolveSafePath(relativePath)
        if (!target.exists()) {
            target.mkdirs()
        }
        return target
    }

    fun moveFile(sourceRel: String, destRel: String, strategy: CollisionStrategy): File? {
        val src = gatekeeper.resolveSafePath(sourceRel)
        var dst = gatekeeper.resolveSafePath(destRel)

        dst = CollisionResolver.resolve(dst, strategy) ?: return null

        val parent = dst.parentFile
        if (parent != null && !parent.exists()) {
            parent.mkdirs()
        }

        if (!src.renameTo(dst)) {
            src.copyTo(dst, overwrite = false)
            src.delete()
        }
        return dst
    }

    fun copyFile(sourceRel: String, destRel: String, strategy: CollisionStrategy): File? {
        val src = gatekeeper.resolveSafePath(sourceRel)
        var dst = gatekeeper.resolveSafePath(destRel)

        dst = CollisionResolver.resolve(dst, strategy) ?: return null

        val parent = dst.parentFile
        if (parent != null && !parent.exists()) {
            parent.mkdirs()
        }

        src.copyTo(dst, overwrite = false)
        return dst
    }

    fun trashFile(sourceRel: String): File {
        val src = gatekeeper.resolveSafePath(sourceRel)
        return trashVault.trashFile(src)
    }

    fun removeDirIfEmpty(relativePath: String): Boolean {
        val target = gatekeeper.resolveSafePath(relativePath)
        return if (target.exists() && target.isDirectory && target.list()?.isEmpty() == true) {
            target.delete()
        } else {
            false
        }
    }
}
