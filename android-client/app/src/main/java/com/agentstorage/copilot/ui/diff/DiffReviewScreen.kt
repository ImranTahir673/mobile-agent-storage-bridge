package com.agentstorage.copilot.ui.diff

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.agentstorage.copilot.data.model.ActionPlan

@Composable
fun DiffReviewScreen(
    plan: ActionPlan,
    onConfirmExecution: (ActionPlan) -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier
) {
    val selectionMap = remember(plan) {
        mutableStateMapOf<String, Boolean>().apply {
            plan.actions.forEach { put(it.actionId, true) }
        }
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(16.dp)
    ) {
        Text(
            text = "Review Proposed Plan",
            style = MaterialTheme.typography.titleLarge
        )

        Text(
            text = plan.description,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 4.dp, bottom = 12.dp)
        )

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            val approvedCount = selectionMap.values.count { it }
            Text(
                text = "$approvedCount of ${plan.actions.size} actions selected",
                style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.Bold
            )

            Row {
                TextButton(onClick = { plan.actions.forEach { selectionMap[it.actionId] = true } }) {
                    Text("Select All")
                }
                TextButton(onClick = { plan.actions.forEach { selectionMap[it.actionId] = false } }) {
                    Text("Clear")
                }
            }
        }

        Spacer(modifier = Modifier.height(8.dp))

        LazyColumn(
            modifier = Modifier.weight(1f)
        ) {
            items(plan.actions, key = { it.actionId }) { action ->
                val isSelected = selectionMap[action.actionId] ?: true
                DiffCard(
                    action = action,
                    isSelected = isSelected,
                    onToggleSelect = { selectionMap[action.actionId] = it }
                )
            }
        }

        Spacer(modifier = Modifier.height(16.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            OutlinedButton(
                onClick = onDismiss,
                modifier = Modifier.weight(1f)
            ) {
                Text("Cancel")
            }

            val approvedCount = selectionMap.values.count { it }
            Button(
                onClick = {
                    val filteredActions = plan.actions.filter { selectionMap[it.actionId] == true }
                    onConfirmExecution(plan.copy(actions = filteredActions))
                },
                enabled = approvedCount > 0,
                modifier = Modifier.weight(2f),
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.primary
                )
            ) {
                Text("Execute ($approvedCount Actions)")
            }
        }
    }
}
