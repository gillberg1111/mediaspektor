import os
import sys

# Change to project dir
sys.path.insert(0, "/home/jakwgrav/Projects/mediaspektor")
from mediaspektor import MediaSpektor

ms = MediaSpektor("config.yaml")
print(ms.db.get_stats())

db_item = ms.db.get_item("plex", "49711")
print("DB ITEM:", db_item)

res = ms.regenerate_item("plex", "49711", "poster")
print("RESULT:", res)
