import os
import json
from datetime import datetime, timedelta

# Create test files with distinct modification timestamps to simulate:
# 1. Today (current time)
# 2. Yesterday (1 day ago)
# 3. Last week (5 days ago)
# 4. Old (45 days ago)

now = datetime.now()
test_dir = os.path.join(os.path.dirname(__file__), "temporal_test_fixtures")
os.makedirs(test_dir, exist_ok=True)

fixtures = [
    ("today_lecture_notes.pdf", 0),                 # Today
    ("yesterday_receipt.pdf", 1),                   # Yesterday
    ("last_week_assignment.pdf", 5),               # 5 days ago
    ("old_archive_2025_project.zip", 45),           # 45 days ago
    ("very_old_tax_return_2024.pdf", 120),          # 120 days ago
]

for fname, days_ago in fixtures:
    fpath = os.path.join(test_dir, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(f"Sample content for {fname} created {days_ago} days ago.")
    target_time = (now - timedelta(days=days_ago)).timestamp()
    os.utime(fpath, (target_time, target_time))

print("Created temporal test fixtures successfully!")
for fname, days_ago in fixtures:
    fpath = os.path.join(test_dir, fname)
    mtime = os.stat(fpath).st_mtime
    dt = datetime.fromtimestamp(mtime)
    print(f"  - {fname}: mtime={dt.strftime('%Y-%m-%d %H:%M')}")
