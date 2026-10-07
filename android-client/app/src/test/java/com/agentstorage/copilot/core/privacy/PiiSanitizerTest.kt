package com.agentstorage.copilot.core.privacy

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PiiSanitizerTest {

    @Test
    fun testTier1ZeroToleranceRedaction_CNIC() {
        val input = "Student CNIC: 37405-1234567-1 was submitted for verification."
        val sanitized = PiiSanitizer.sanitizeSnippet(input)

        assertEquals("Student CNIC: [REDACTED_CNIC] was submitted for verification.", sanitized)
        assertFalse(sanitized.contains("37405-1234567-1"))
    }

    @Test
    fun testTier1ZeroToleranceRedaction_PhoneNumbers() {
        val input1 = "Call me at 0300-1234567 regarding the lab."
        val sanitized1 = PiiSanitizer.sanitizeSnippet(input1)
        assertEquals("Call me at [REDACTED_PHONE] regarding the lab.", sanitized1)

        val input2 = "International phone: +923001234567 or 0321 7654321."
        val sanitized2 = PiiSanitizer.sanitizeSnippet(input2)
        assertEquals("International phone: [REDACTED_PHONE] or [REDACTED_PHONE].", sanitized2)
    }

    @Test
    fun testTier1ZeroToleranceRedaction_PaymentCardsAndIbans() {
        val cardInput = "Paid via Visa: 4111 2222 3333 4444 online."
        val sanitizedCard = PiiSanitizer.sanitizeSnippet(cardInput)
        assertEquals("Paid via Visa: [REDACTED_CARD] online.", sanitizedCard)

        val ibanInput = "Transfer to HBL account: PK36HABB0000123456789012 directly."
        val sanitizedIban = PiiSanitizer.sanitizeSnippet(ibanInput)
        assertEquals("Transfer to HBL account: [REDACTED_IBAN] directly.", sanitizedIban)
    }

    @Test
    fun testTier1ZeroToleranceRedaction_CredentialsAndEmails() {
        val input = "Email: student@nu.edu.pk, password: mySecretPassword123"
        val sanitized = PiiSanitizer.sanitizeSnippet(input)

        assertTrue(sanitized.contains("[REDACTED_EMAIL]"))
        assertTrue(sanitized.contains("[REDACTED_SECRET]"))
        assertFalse(sanitized.contains("student@nu.edu.pk"))
        assertFalse(sanitized.contains("mySecretPassword123"))
    }

    @Test
    fun testTier2ContextPreservation_AcademicAndVoucherData() {
        val voucherInput = """
            Student Fee Challan - Fall Semester 2026
            Student Name: Imran Tahir
            Roll No: BSAI-182
            Course: CS-301 Machine Learning Lab
            Voucher No: 182-9021-A
            Total Payable: PKR 145,000
            Due Date: 2026-10-15
        """.trimIndent()

        val sanitized = PiiSanitizer.sanitizeSnippet(voucherInput)

        // Tier 2 safe tokens must be 100% retained in plaintext
        assertTrue("Roll No must be preserved", sanitized.contains("Roll No: BSAI-182"))
        assertTrue("Course code must be preserved", sanitized.contains("CS-301"))
        assertTrue("Voucher identifier must be preserved", sanitized.contains("182-9021-A"))
        assertTrue("Amount must be preserved", sanitized.contains("PKR 145,000"))
        assertTrue("Academic term must be preserved", sanitized.contains("Fall Semester 2026"))
        assertTrue("Student name must be preserved for profile routing", sanitized.contains("Imran Tahir"))
    }

    @Test
    fun testContainsTier1Pii() {
        assertTrue(PiiSanitizer.containsTier1Pii("receipt_37405-1234567-1.pdf"))
        assertTrue(PiiSanitizer.containsTier1Pii("payment_4111222233334444.pdf"))
        assertTrue(PiiSanitizer.containsTier1Pii("contact_03001234567.txt"))

        assertFalse(PiiSanitizer.containsTier1Pii("Deep_Learning_Lecture_Notes.pdf"))
        assertFalse(PiiSanitizer.containsTier1Pii("Imran_Tahir_BSAI182_Fee_Voucher.pdf"))
        assertFalse(PiiSanitizer.containsTier1Pii("Documents/University/CS301_Assignment_1.pdf"))
    }
}
