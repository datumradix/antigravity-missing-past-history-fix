The 100-item index cap (trajectorySummaries): The IDE keeps an internal cache list in AppData/Roaming/Antigravity IDE/User/globalStorage/state.vscdb (antigravityUnifiedStateSync.trajectorySummaries). This cache is capped at exactly 100 items and froze as soon as it reach 100 convo.
The IDE's conversation index (antigravityUnifiedStateSync.trajectorySummaries in state.vscdb) had a fixed capacity of 100 items. Because it was operating as FIFO (First-In, First-Out) without evicting obsolete sessions, the list capped out on reaching 100 convo.
This script applies LIFO Index:

Safely created a backup of your storage database at: %APPDATA%/Antigravity IDE/User/globalStorage/state.vscdb.backup_before_lifo_fix
Rebuilt the index using LIFO (Last-In, First-Out) ordering:
Evicted the stale entries.
Extracted, synthesized, and encoded the summaries for missing recent .db sessions.
Re-indexed the top 100 latest conversations sorted chronologically (newest at the head).

You can run it anytime with:
bash
python sync_conversation_history.py
(Supports --dry-run to preview without modifying, and --limit N to adjust the window size).

How to Refresh the IDE UI Now
Because the IDE caches state in memory while running, reload the window to have it read the updated index:

Press Ctrl + Shift + P
Type Developer: Reload Window and press Enter (or simply restart Antigravity IDE).
Your conversation list and past conversations picker will now display all your recent sessions
