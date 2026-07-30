#!/usr/bin/env bash
# Pull the bot's data off the server and merge it into a local master archive.
# The server keeps only the retention window; this archive keeps everything.
#
# Usage: backup_pull.sh [local-archive-dir]
set -euo pipefail

REMOTE="${REMOTE:-vps}"
REMOTE_DIR="${REMOTE_DIR:-/opt/bots/tg-reminder-bot/data}"
LOCAL_DIR="${1:-$HOME/Archive/tg-reminder-bot}"

SNAPSHOT_DIR="$LOCAL_DIR/incoming"
MASTER="$LOCAL_DIR/master.sqlite3"

mkdir -p "$SNAPSHOT_DIR" "$LOCAL_DIR/media"

echo "==> pulling database and media from $REMOTE"
rsync -az --timeout=120 "$REMOTE:$REMOTE_DIR/reminders.sqlite3" "$SNAPSHOT_DIR/snapshot.sqlite3"
rsync -az --timeout=120 --ignore-existing "$REMOTE:$REMOTE_DIR/media/" "$LOCAL_DIR/media/" || true

echo "==> merging into $MASTER"
sqlite3 "$MASTER" <<SQL
ATTACH DATABASE '$SNAPSHOT_DIR/snapshot.sqlite3' AS src;

CREATE TABLE IF NOT EXISTS owners AS SELECT * FROM src.owners WHERE 0;
CREATE TABLE IF NOT EXISTS messages AS SELECT * FROM src.messages WHERE 0;
CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_key
  ON messages(owner_id, chat_id, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_owners_conn
  ON owners(business_connection_id);

INSERT OR IGNORE INTO owners SELECT * FROM src.owners;
INSERT OR IGNORE INTO messages SELECT * FROM src.messages;

DETACH DATABASE src;
SQL

COUNT=$(sqlite3 "$MASTER" "SELECT COUNT(*) FROM messages;")
echo "==> master archive now holds $COUNT messages"
