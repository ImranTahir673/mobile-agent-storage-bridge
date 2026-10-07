package com.agentstorage.copilot.core.privacy

import java.util.regex.Pattern

/**
 * Pre-Flight RAM-Level PII Sanitizer.
 * Enforces Rule 6 (Tier 1 vs. Tier 2 Privacy Shield):
 * Scrubs all Tier 1 sensitive personal data from text snippets before they are transmitted
 * over network APIs or processed by reasoning models.
 */
object PiiSanitizer {

    // Tier 1 Zero-Tolerance Redaction Patterns
    private val IBAN_PATTERN = Pattern.compile("\\b[A-Z]{2}\\d{2}[A-Z0-9]{11,30}\\b")
    private val CARD_PATTERN = Pattern.compile("\\b(?:\\d{4}[- ]?){3}\\d{4}\\b")
    private val CNIC_PATTERN = Pattern.compile("\\b\\d{5}-\\d{7}-\\d\\b")
    private val PASSPORT_PATTERN = Pattern.compile("\\b[A-PR-WY][1-9]\\d\\s?\\d{4}[1-9]\\b")
    private val PHONE_PATTERN = Pattern.compile("(?i)(?:\\+92[- ]?3\\d{2}[- ]?\\d{7}\\b|\\b0?3\\d{2}[- ]?\\d{7}\\b)")
    private val EMAIL_PATTERN = Pattern.compile("\\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}\\b")
    private val CREDENTIALS_PATTERN = Pattern.compile("(?i)(password|passwd|pin|secret|token|api_key)\\s*[:=]\\s*\\S+")

    /**
     * Sanitizes a text snippet in RAM before serialization.
     * Replaces Tier 1 sensitive patterns with non-reversible redaction tokens.
     */
    fun sanitizeSnippet(rawText: String): String {
        if (rawText.isBlank()) return ""

        var sanitized = rawText
        sanitized = IBAN_PATTERN.matcher(sanitized).replaceAll("[REDACTED_IBAN]")
        sanitized = CARD_PATTERN.matcher(sanitized).replaceAll("[REDACTED_CARD]")
        sanitized = CNIC_PATTERN.matcher(sanitized).replaceAll("[REDACTED_CNIC]")
        sanitized = PASSPORT_PATTERN.matcher(sanitized).replaceAll("[REDACTED_ID]")
        sanitized = PHONE_PATTERN.matcher(sanitized).replaceAll("[REDACTED_PHONE]")
        sanitized = EMAIL_PATTERN.matcher(sanitized).replaceAll("[REDACTED_EMAIL]")
        sanitized = CREDENTIALS_PATTERN.matcher(sanitized).replaceAll("$1: [REDACTED_SECRET]")

        return sanitized
    }

    /**
     * Validates whether a proposed destination path or filename contains unredacted Tier 1 PII.
     * Returns true if any unredacted PII is detected.
     */
    fun containsTier1Pii(pathOrName: String): Boolean {
        if (pathOrName.isBlank()) return false
        return CNIC_PATTERN.matcher(pathOrName).find() ||
                PHONE_PATTERN.matcher(pathOrName).find() ||
                CARD_PATTERN.matcher(pathOrName).find() ||
                IBAN_PATTERN.matcher(pathOrName).find() ||
                EMAIL_PATTERN.matcher(pathOrName).find() ||
                PASSPORT_PATTERN.matcher(pathOrName).find()
    }
}
