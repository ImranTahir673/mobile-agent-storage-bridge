package com.agentstorage.copilot.core.gatekeeper

import com.agentstorage.copilot.core.privacy.PiiSanitizer
import com.agentstorage.copilot.data.model.ActionPlan
import com.agentstorage.copilot.data.model.PlanAction
import com.agentstorage.copilot.data.model.UserProfile
import com.agentstorage.copilot.data.model.ValidationResult
import java.io.File

class PolicyGatekeeper(private val baseDir: File) {

    private val canonicalBaseDir: String by lazy {
        runCatching { baseDir.canonicalPath }.getOrDefault(baseDir.absolutePath)
    }

    companion object {
        const val MAX_ACTIONS_PER_BATCH = 20
        const val MAX_CUMULATIVE_BYTES = 500L * 1024L * 1024L // 500 MB
        const val MAX_NESTING_DEPTH = 5

        private val PROHIBITED_EXTENSIONS = setOf("sh", "apk", "dex", "so", "bin")
    }

    /**
     * Resolves a relative path safely within the baseDir boundary.
     * Throws SecurityException if path traversal or boundary escape occurs.
     */
    fun resolveSafePath(relativePath: String): File {
        val clean = relativePath.trim().trimStart('/', '\\')
        val target = File(baseDir, clean)
        val canonicalTarget = target.canonicalPath

        if (!canonicalTarget.startsWith(canonicalBaseDir)) {
            throw SecurityException("Path containment violation: $relativePath escapes base directory.")
        }
        return target
    }

    /**
     * Checks if a resolved path violates protected blacklists.
     */
    fun isBlacklisted(file: File): Boolean {
        val canonical = file.canonicalPath
        val relative = if (canonical.startsWith(canonicalBaseDir)) {
            canonical.removePrefix(canonicalBaseDir).trimStart(File.separatorChar)
        } else {
            return true
        }

        val parts = relative.split(File.separatorChar)
        if (parts.isEmpty() || parts[0].isEmpty()) return false

        val rootFirst = parts[0]
        if (rootFirst.equals("Android", ignoreCase = true)) return true
        if (rootFirst.equals(".agent_trash", ignoreCase = true)) return true
        if (rootFirst.startsWith(".git", ignoreCase = true)) return true
        if (rootFirst.startsWith(".ledger.db", ignoreCase = true)) return true
        if (rootFirst.startsWith(".") && parts.size == 1 && rootFirst.endsWith(".db")) return true

        return false
    }

    /**
     * Validates an entire ActionPlan against all Policy Gatekeeper rules.
     */
    fun validatePlan(plan: ActionPlan, userProfile: UserProfile? = null): ValidationResult {
        // Rule 3: Blast Radius Limits
        if (plan.actions.isEmpty()) {
            return ValidationResult.Failure("Action plan contains no operations.")
        }
        if (plan.actions.size > MAX_ACTIONS_PER_BATCH) {
            return ValidationResult.Failure(
                "Blast radius exceeded: Batch contains ${plan.actions.size} actions (Max allowed: $MAX_ACTIONS_PER_BATCH)."
            )
        }

        val vettedActions = mutableListOf<PlanAction>()

        for (action in plan.actions) {
            // Rule 4: Coerce hard deletes to soft-delete trash
            val normalizedType = if (action.type.equals("delete", ignoreCase = true)) "trash" else action.type

            // Check paths
            when (normalizedType) {
                "make_dir" -> {
                    val pathStr = action.path ?: return ValidationResult.Failure("make_dir missing target path.")
                    val target = resolveSafePath(pathStr)
                    if (isBlacklisted(target)) {
                        return ValidationResult.Failure("Target path is blacklisted: $pathStr")
                    }
                    if (pathStr.split('/', '\\').size > MAX_NESTING_DEPTH) {
                        return ValidationResult.Failure("Nesting depth exceeded for directory: $pathStr")
                    }
                }
                "trash" -> {
                    val pathStr = action.path ?: return ValidationResult.Failure("trash missing target path.")
                    val target = resolveSafePath(pathStr)
                    if (isBlacklisted(target)) {
                        return ValidationResult.Failure("Target path is blacklisted: $pathStr")
                    }
                }
                "move", "copy" -> {
                    val srcStr = action.source ?: return ValidationResult.Failure("$normalizedType missing source path.")
                    val dstStr = action.destination ?: return ValidationResult.Failure("$normalizedType missing destination path.")

                    val srcFile = resolveSafePath(srcStr)
                    val dstFile = resolveSafePath(dstStr)

                    if (isBlacklisted(srcFile)) return ValidationResult.Failure("Source path is blacklisted: $srcStr")
                    if (isBlacklisted(dstFile)) return ValidationResult.Failure("Destination path is blacklisted: $dstStr")

                    // Prohibited executable binaries
                    if (PROHIBITED_EXTENSIONS.contains(dstFile.extension.lowercase())) {
                        return ValidationResult.Failure("Forbidden file extension: .${dstFile.extension}")
                    }

                    // Rule 6: PII in destination name
                    if (PiiSanitizer.containsTier1Pii(dstFile.name)) {
                        return ValidationResult.Failure("Proposed destination contains unredacted Tier 1 PII: ${dstFile.name}")
                    }

                    // Rule 9: Peer Separation
                    if (userProfile != null && violatesPeerSeparation(srcStr, dstStr, userProfile)) {
                        return ValidationResult.Failure("Peer document routed into personal directory: $srcStr -> $dstStr")
                    }
                }
                else -> {
                    return ValidationResult.Failure("Unsupported operation type: ${action.type}")
                }
            }

            vettedActions.add(action.copy(type = normalizedType))
        }

        return ValidationResult.Success(plan.copy(actions = vettedActions))
    }

    private fun violatesPeerSeparation(source: String, destination: String, profile: UserProfile): Boolean {
        val destNormalized = destination.replace('\\', '/')
        val personalBase = profile.routingRules.personalDocumentsBase.replace('\\', '/')

        if (destNormalized.startsWith(personalBase, ignoreCase = true)) {
            val srcLower = source.lowercase()
            for (peer in profile.knownPeers) {
                if (srcLower.contains(peer.name.lowercase())) return true
                for (alias in peer.aliases) {
                    if (srcLower.contains(alias.lowercase())) return true
                }
            }
        }
        return false
    }
}
