---
name: Google Drive backup via rclone
description: How to back up the radio-gateway repo to Google Drive using rclone
type: reference
---

rclone is configured with a `gdrive:` remote pointing to the user's Google Drive.

**Backup command:**
```bash
rclone copy /home/user/Downloads/radio-gateway gdrive:radio-gateway-backup/repo \
  --exclude ".git/**" --exclude "__pycache__/**" --exclude "*.pyc" \
  --exclude "recordings/**" --exclude "audio/**" -v
```

**Also back up the config (not in repo):**
```bash
rclone copy /home/user/Downloads/radio-gateway/gateway_config.txt gdrive:radio-gateway-backup/config/
```

**Verify:**
```bash
rclone size gdrive:radio-gateway-backup/repo/
```

**Restore:**
```bash
rclone copy gdrive:radio-gateway-backup/repo/ /home/user/Downloads/radio-gateway/ -v
```
