#!/usr/bin/env bash
# Pull the bot's data off the server and merge it into a local master archive.
# The server keeps only the retention window; this archive keeps everything.
#
# Usage: backup_pull.sh [local-archive-dir]
set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${SELF_DIR%/scripts}"
AGENT_PLIST="$HOME/Library/LaunchAgents/com.nkonshin.reminderbot.backup.plist"

# The shipped plist carries a REPLACE_WITH_ABSOLUTE_PATH placeholder in its
# ProgramArguments. `launchctl load` accepts that happily, and the agent then
# fails every Sunday into /tmp/reminderbot-backup.err — a log nobody reads —
# so the archive silently stops updating and nothing says why. This runs where
# a human is actually looking at the output.
if [[ -f "$AGENT_PLIST" ]]; then
  # Read the argument itself rather than grepping the file: the comment in the
  # shipped plist mentions the script name too.
  agent_script="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:1' \
                    "$AGENT_PLIST" 2>/dev/null || true)"
  if [[ "$agent_script" != *backup_pull.sh ]]; then
    echo "!! $AGENT_PLIST does not point at backup_pull.sh at all — the weekly" >&2
    echo "   backup cannot be running. Reinstall it from scripts/." >&2
    exit 1
  fi
  if [[ "$agent_script" == *REPLACE_WITH_ABSOLUTE_PATH* ]]; then
    echo "!! The installed launchd agent still contains REPLACE_WITH_ABSOLUTE_PATH," >&2
    echo "   so the weekly backup has never actually run. Fix it with:" >&2
    echo "     sed -i '' 's|REPLACE_WITH_ABSOLUTE_PATH|$REPO_DIR|' '$AGENT_PLIST'" >&2
    echo "     launchctl unload '$AGENT_PLIST' 2>/dev/null || true" >&2
    echo "     launchctl load '$AGENT_PLIST'" >&2
    exit 1
  fi
  if [[ ! -f "$agent_script" ]]; then
    echo "!! The installed launchd agent points at a script that does not exist:" >&2
    echo "     $agent_script" >&2
    echo "   Point it at $SELF_DIR/backup_pull.sh and reload it." >&2
    exit 1
  fi
fi

REMOTE="${REMOTE:-vps}"
REMOTE_DIR="${REMOTE_DIR:-/opt/bots/tg-reminder-bot/data}"
LOCAL_DIR="${1:-$HOME/Archive/tg-reminder-bot}"

SNAPSHOT_DIR="$LOCAL_DIR/incoming"
MASTER="$LOCAL_DIR/master.sqlite3"

mkdir -p "$SNAPSHOT_DIR" "$LOCAL_DIR/media"

echo "==> pulling database and media from $REMOTE"
# NOTE: only the main .sqlite3 file is pulled, not its -wal/-shm sidecars. The
# bot runs in WAL mode, so the newest committed rows can still be sitting in
# the -wal file at the moment of the rsync; they arrive with the following
# week's pull. Deliberately left alone for now: doing it properly means asking
# the server for a checkpointed copy (the way /admin -> Экспорт already does,
# via SQLite's backup API) rather than rsyncing a live file.
rsync -az --timeout=120 "$REMOTE:$REMOTE_DIR/reminders.sqlite3" "$SNAPSHOT_DIR/snapshot.sqlite3"

# The media directory does not exist until the first file is downloaded, and
# rsync exits non-zero when the source is missing. `|| true` used to paper over
# that -- but it swallowed genuine auth and network failures just as happily,
# which is what let months of missing media go unnoticed. Probe first, so only
# "not there yet" is tolerated.
set +e
ssh -o BatchMode=yes "$REMOTE" "test -d '$REMOTE_DIR/media'"
probe=$?
set -e
case "$probe" in
  0) rsync -az --timeout=120 --ignore-existing \
       "$REMOTE:$REMOTE_DIR/media/" "$LOCAL_DIR/media/" ;;
  1) echo "==> no media directory on the server yet — nothing to pull" ;;
  *) echo "!! cannot reach $REMOTE (ssh exited $probe) — refusing to report" >&2
     echo "   a successful backup that copied nothing" >&2
     exit "$probe" ;;
esac

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

-- A message already archived keeps its first-seen text forever (that is the
-- whole point: an edit destroys the original, so the pre-edit version is
-- exactly what is worth keeping). But the fact that it was later edited or
-- deleted must not be lost just because the row was already archived before
-- that happened -- so backfill the timestamps from this snapshot.
-- COALESCE(local, remote) makes this idempotent: once a timestamp is set
-- locally it is never rewritten, so re-running the merge never discards
-- information the archive already has.
UPDATE messages SET
  edited_at = COALESCE(edited_at, (
    SELECT s.edited_at FROM src.messages s
     WHERE s.owner_id = messages.owner_id
       AND s.chat_id = messages.chat_id
       AND s.message_id = messages.message_id)),
  deleted_at = COALESCE(deleted_at, (
    SELECT s.deleted_at FROM src.messages s
     WHERE s.owner_id = messages.owner_id
       AND s.chat_id = messages.chat_id
       AND s.message_id = messages.message_id))
WHERE EXISTS (
  SELECT 1 FROM src.messages s
   WHERE s.owner_id = messages.owner_id
     AND s.chat_id = messages.chat_id
     AND s.message_id = messages.message_id);

DETACH DATABASE src;
SQL

COUNT=$(sqlite3 "$MASTER" "SELECT COUNT(*) FROM messages;")
echo "==> master archive now holds $COUNT messages"
