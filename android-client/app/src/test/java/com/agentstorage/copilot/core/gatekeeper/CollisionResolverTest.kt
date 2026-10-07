package com.agentstorage.copilot.core.gatekeeper

import com.agentstorage.copilot.data.model.CollisionStrategy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class CollisionResolverTest {

    @get:Rule
    val tempFolder = TemporaryFolder()

    @Test
    fun testNoCollision_ReturnsOriginalDestinationDirectly() {
        val root = tempFolder.newFolder("target_dir")
        val destination = File(root, "unique_document.pdf")

        val resolved = CollisionResolver.resolve(destination, CollisionStrategy.FAIL)
        assertNotNull(resolved)
        assertEquals(destination.absolutePath, resolved?.absolutePath)
    }

    @Test
    fun testFailStrategy_ThrowsIllegalStateExceptionWhenFileExists() {
        val root = tempFolder.newFolder("target_fail")
        val existing = File(root, "colliding_file.pdf").apply { writeText("original") }

        try {
            CollisionResolver.resolve(existing, CollisionStrategy.FAIL)
            fail("Expected IllegalStateException for FAIL collision strategy")
        } catch (e: IllegalStateException) {
            assertTrue(e.message?.contains("Destination collision detected") == true)
        }
    }

    @Test
    fun testSkipStrategy_ReturnsNullWhenFileExists() {
        val root = tempFolder.newFolder("target_skip")
        val existing = File(root, "colliding_file.pdf").apply { writeText("original") }

        val resolved = CollisionResolver.resolve(existing, CollisionStrategy.SKIP)
        assertNull("SKIP strategy must return null to omit operation", resolved)
    }

    @Test
    fun testRenameNumericStrategy_AppendsIncrementingNumericCounter() {
        val root = tempFolder.newFolder("target_numeric")
        val existing0 = File(root, "report.pdf").apply { writeText("0") }
        File(root, "report (1).pdf").writeText("1")

        val resolved = CollisionResolver.resolve(existing0, CollisionStrategy.RENAME_NUMERIC)
        assertNotNull(resolved)
        assertEquals("report (2).pdf", resolved?.name)
        assertFalse(resolved!!.exists())
    }

    @Test
    fun testRenameTimestampStrategy_AppendsIsoTimestamp() {
        val root = tempFolder.newFolder("target_timestamp")
        val existing = File(root, "invoice.pdf").apply { writeText("invoice content") }

        val resolved = CollisionResolver.resolve(existing, CollisionStrategy.RENAME_TIMESTAMP)
        assertNotNull(resolved)
        assertTrue(resolved!!.name.startsWith("invoice_"))
        assertTrue(resolved.name.endsWith(".pdf"))
        assertFalse(resolved.exists())
    }
}
