package com.agentstorage.copilot

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import com.agentstorage.copilot.ui.home.HomeScreen
import com.agentstorage.copilot.ui.home.HomeViewModel
import com.agentstorage.copilot.ui.theme.AgentStorageCopilotTheme

class MainActivity : ComponentActivity() {

    private val homeViewModel: HomeViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        checkAndRequestStoragePermissions()

        setContent {
            AgentStorageCopilotTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    // Uses API key from BuildConfig or environment/settings
                    val apiKey = getApiKey()
                    HomeScreen(viewModel = homeViewModel, apiKey = apiKey)
                }
            }
        }
    }

    private fun checkAndRequestStoragePermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (!Environment.isExternalStorageManager()) {
                val intent = Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION).apply {
                    data = Uri.parse("package:$packageName")
                }
                startActivity(intent)
            }
        }
    }

    private fun getApiKey(): String {
        // Fallback or read from secure preferences
        val prefs = getSharedPreferences("copilot_settings", MODE_PRIVATE)
        return prefs.getString("gemini_api_key", "") ?: ""
    }
}
