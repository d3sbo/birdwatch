import sqlite3
import sys

# Usage: python check_shed.py "Camera 1"
camera = sys.argv[1] if len(sys.argv) > 1 else "Camera 1"

conn = sqlite3.connect('/data/birdwatch.db')
rows = conn.execute("SELECT common_name, timestamp FROM detections WHERE camera=? ORDER BY timestamp DESC LIMIT 5", (camera,)).fetchall()
print(f'Recent {camera} detections:')
for r in rows: print(' ', r)
total = conn.execute("SELECT COUNT(*) FROM detections WHERE camera=?", (camera,)).fetchone()[0]
print('Total ever:', total)
today = conn.execute("SELECT COUNT(*) FROM detections WHERE camera=? AND date=date('now','localtime')", (camera,)).fetchone()[0]
print('Today:', today)
