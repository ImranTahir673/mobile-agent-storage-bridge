package com.agentstorage.copilot.data.remote

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class GeminiRequest(
    @SerialName("contents")
    val contents: List<GeminiContent>,
    @SerialName("system_instruction")
    val systemInstruction: GeminiContent? = null,
    @SerialName("generationConfig")
    val generationConfig: GeminiGenerationConfig? = null
)

@Serializable
data class GeminiContent(
    @SerialName("role")
    val role: String? = null,
    @SerialName("parts")
    val parts: List<GeminiPart>
)

@Serializable
data class GeminiPart(
    @SerialName("text")
    val text: String? = null
)

@Serializable
data class GeminiGenerationConfig(
    @SerialName("temperature")
    val temperature: Float = 0.2f
)

@Serializable
data class GeminiResponse(
    @SerialName("candidates")
    val candidates: List<GeminiCandidate>? = null
)

@Serializable
data class GeminiCandidate(
    @SerialName("content")
    val content: GeminiContent? = null
)
