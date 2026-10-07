package com.agentstorage.copilot.core.gatekeeper

import com.agentstorage.copilot.data.model.ActionPlan
import com.agentstorage.copilot.data.model.PeerProfile
import com.agentstorage.copilot.data.model.PlanAction
import com.agentstorage.copilot.data.model.RoutingRules
import com.agentstorage.copilot.data.model.UserIdentity
import com.agentstorage.copilot.data.model.UserProfile
import com.agentstorage.copilot.data.model.ValidationResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class PolicyGatekeeperTest {

    @get:Rule
    val tempFolder = TemporaryFolder()

    private lateinit var baseDir: File
    private lateinit var gatekeeper: PolicyGatekeeper
    private lateinit var sampleProfile: UserProfile

    @Before
    fun setUp() {
        baseDir = tempFolder.newFolder("storage_root")
        gatekeeper = PolicyGatekeeper(baseDir)
        sampleProfile = UserProfile(
            userIdentity = UserIdentity(primaryName = "Imran Tahir"),
            knownPeers = listOf(
                PeerProfile(
                    name = "Fawad",
                    aliases = listOf("fawad", "fawad_khan"),
                    designatedFolder = "Documents/Peers/Fawad"
                )
            ),
            routingRules = RoutingRules(
                peerDocumentsBase = "Documents/Peers",
                personalDocumentsBase = "Documents/Personal",
                academicBase = "Documents/University"
            )
        )
    }

    @Test
    fun testPathContainment_NormalPathResolvesSafely() {
        val file = gatekeeper.resolveSafePath("Download/sample.pdf")
        assertTrue(file.canonicalPath.startsWith(baseDir.canonicalPath))
        assertEquals("sample.pdf", file.name)
    }

    @Test
    fun testPathContainment_DirectoryTraversalThrowsSecurityException() {
        try {
            gatekeeper.resolveSafePath("../../etc/passwd")
            fail("Expected SecurityException on directory traversal")
        } catch (e: SecurityException) {
            assertTrue(e.message?.contains("Path containment violation") == true)
        }

        try {
            gatekeeper.resolveSafePath("Download/../../outside.txt")
            fail("Expected SecurityException on directory traversal")
        } catch (e: SecurityException) {
            assertTrue(e.message?.contains("Path containment violation") == true)
        }
    }

    @Test
    fun testBlacklistEnforcement_ProtectedSystemAndMetadataEntities() {
        val androidFolder = File(baseDir, "Android/data")
        val gitFolder = File(baseDir, ".git/hooks")
        val trashFolder = File(baseDir, ".agent_trash/vault")
        val ledgerDb = File(baseDir, ".ledger.db")

        assertTrue(gatekeeper.isBlacklisted(androidFolder))
        assertTrue(gatekeeper.isBlacklisted(gitFolder))
        assertTrue(gatekeeper.isBlacklisted(trashFolder))
        assertTrue(gatekeeper.isBlacklisted(ledgerDb))

        val regularDoc = File(baseDir, "Documents/Research/paper.pdf")
        assertFalse(gatekeeper.isBlacklisted(regularDoc))
    }

    @Test
    fun testBlastRadiusEnforcement_RejectsMoreThan20Actions() {
        val actions = (1..21).map { idx ->
            PlanAction(
                actionId = "step-$idx",
                type = "make_dir",
                path = "Documents/Batch_$idx"
            )
        }

        val plan = ActionPlan(
            planId = "test-plan-id",
            description = "Bulk creation of 21 folders",
            actions = actions
        )

        val result = gatekeeper.validatePlan(plan)
        assertTrue(result is ValidationResult.Failure)
        val failure = result as ValidationResult.Failure
        assertTrue(failure.reason.contains("Blast radius exceeded"))
    }

    @Test
    fun testZeroHardDeleteCoercion_CoercesDeleteToTrash() {
        val plan = ActionPlan(
            planId = "test-delete-plan",
            description = "Remove obsolete cached receipt",
            actions = listOf(
                PlanAction(
                    actionId = "step-1",
                    type = "delete",
                    path = "Download/old_cached_temp.pdf"
                )
            )
        )

        val result = gatekeeper.validatePlan(plan)
        assertTrue(result is ValidationResult.Success)
        val vetted = (result as ValidationResult.Success).plan
        assertEquals("trash", vetted.actions.first().type)
    }

    @Test
    fun testPeerSeparationEnforcement_RejectsPeerDocumentInPersonalDirectory() {
        val plan = ActionPlan(
            planId = "test-peer-plan",
            description = "Move peer voucher into personal folder",
            actions = listOf(
                PlanAction(
                    actionId = "step-1",
                    type = "move",
                    source = "Download/fawad fee challan.pdf",
                    destination = "Documents/Personal/Receipts/fawad fee challan.pdf"
                )
            )
        )

        val result = gatekeeper.validatePlan(plan, sampleProfile)
        assertTrue(result is ValidationResult.Failure)
        val failure = result as ValidationResult.Failure
        assertTrue(failure.reason.contains("Peer document routed into personal directory"))
    }
}
