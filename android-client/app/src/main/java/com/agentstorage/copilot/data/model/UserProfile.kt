package com.agentstorage.copilot.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class UserProfile(
    @SerialName("user_identity")
    val userIdentity: UserIdentity = UserIdentity(),
    @SerialName("known_peers")
    val knownPeers: List<PeerProfile> = emptyList(),
    @SerialName("routing_rules")
    val routingRules: RoutingRules = RoutingRules()
)

@Serializable
data class UserIdentity(
    @SerialName("primary_name")
    val primaryName: String = "User",
    @SerialName("aliases")
    val aliases: List<String> = emptyList(),
    @SerialName("identifiers")
    val identifiers: List<String> = emptyList(),
    @SerialName("organization")
    val organization: String = ""
)

@Serializable
data class PeerProfile(
    @SerialName("name")
    val name: String,
    @SerialName("aliases")
    val aliases: List<String> = emptyList(),
    @SerialName("relation")
    val relation: String = "",
    @SerialName("designated_folder")
    val designatedFolder: String
)

@Serializable
data class RoutingRules(
    @SerialName("peer_documents_base")
    val peerDocumentsBase: String = "Documents/Peers",
    @SerialName("personal_documents_base")
    val personalDocumentsBase: String = "Documents/Personal",
    @SerialName("academic_base")
    val academicBase: String = "Documents/University"
)
