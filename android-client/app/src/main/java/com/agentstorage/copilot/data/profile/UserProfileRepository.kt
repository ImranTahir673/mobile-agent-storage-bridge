package com.agentstorage.copilot.data.profile

import android.content.Context
import com.agentstorage.copilot.data.model.PeerProfile
import com.agentstorage.copilot.data.model.RoutingRules
import com.agentstorage.copilot.data.model.UserIdentity
import com.agentstorage.copilot.data.model.UserProfile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import java.io.File

class UserProfileRepository(private val context: Context, private val baseDir: File) {

    private val json = Json {
        prettyPrint = true
        ignoreUnknownKeys = true
    }

    private val profileFile: File
        get() = File(baseDir, "user_profile.json")

    suspend fun getProfile(): UserProfile = withContext(Dispatchers.IO) {
        if (profileFile.exists()) {
            try {
                val content = profileFile.readText()
                return@withContext json.decodeFromString<UserProfile>(content)
            } catch (e: Exception) {
                // Fallback to default
            }
        }
        createDefaultProfile()
    }

    suspend fun saveProfile(profile: UserProfile) = withContext(Dispatchers.IO) {
        val serialized = json.encodeToString(profile)
        profileFile.writeText(serialized)
    }

    private fun createDefaultProfile(): UserProfile {
        return UserProfile(
            userIdentity = UserIdentity(
                primaryName = "User",
                aliases = listOf("user", "me"),
                identifiers = emptyList(),
                organization = "Personal"
            ),
            knownPeers = listOf(
                PeerProfile(
                    name = "Peer Example",
                    aliases = listOf("colleague", "partner"),
                    relation = "Colleague / Peer",
                    designatedFolder = "Documents/Peers/Peer_Example"
                )
            ),
            routingRules = RoutingRules(
                peerDocumentsBase = "Documents/Peers",
                personalDocumentsBase = "Documents/Personal",
                academicBase = "Documents/University"
            )
        )
    }
}
