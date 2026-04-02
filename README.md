# STLFlix Downloader

Bulk-downloads all STLFlix content to a local folder and syncs new drops on demand. Runs entirely in Docker — no Python install needed.

## Directory structure

Downloads are saved as:
```
<YOUR_DOWNLOAD_DIR>\
├── drop-41\
│   ├── steampunk-topper-hat\
│   │   ├── Steampunk_Topper_Hat_Small.zip
│   │   └── Steampunk_Topper_Hat_Readme.pdf
│   └── minimalist-nativity-set\
│       └── ...
├── drop-42\
│   └── ...
├── extra-drop-135\
│   └── ...
└── community-drop-01\
    └── ...
```

## First-time setup

**1. Build the image**
```bash
git clone <repo-url> && cd stlflix-downloader
docker compose build
```

**2. Create the output folder** (if it doesn't exist)
```bash
mkdir <YOUR_DOWNLOAD_DIR>
```

**3. Create the JWT cache file** (required for Docker volume mount)
```bash
echo {} > .jwt_cache.json
```

That's it. Credentials are already configured in `docker-compose.yml`.

## Usage

### Download everything / sync new drops
```bash
docker compose run --rm stlflix-downloader
```
Re-run this any time. It checks every product against the local manifest and only downloads files that are missing. Already-downloaded files are skipped instantly.

### See what's new without downloading
```bash
docker compose run --rm stlflix-downloader --dry-run
```
Lists every file that would be downloaded without touching disk.

### Watch live progress
```bash
# If running detached (-d):
docker compose run -d --name stlflix-dl stlflix-downloader
docker logs stlflix-dl --follow
```

## How it works

1. Logs in to `k8s.stlflix.com` and caches the JWT for 30 days
2. Fetches all drops + products + file IDs via GraphQL (431 drops across 22 pages)
3. For each file, calls the product-file API to get the S3 URL
4. Downloads up to 5 files concurrently from S3
5. Saves a `.manifest.json` in the download folder to track what's been downloaded
6. On re-run, skips any file whose local path already exists on disk

## Credentials / config

Credentials are in `docker-compose.yml` under `environment:`. To update them:

```yaml
environment:
  - STLFLIX_EMAIL=your@email.com
  - STLFLIX_PASSWORD=yourpassword    # use $$ for a literal $ in the password
  - DOWNLOAD_DIR=/downloads
```

To change the download folder, update the volume mount in `docker-compose.yml`:
```yaml
volumes:
  - <YOUR_DOWNLOAD_DIR>:/downloads    # change the left side to your actual path
```

## Files

| File | Purpose |
|------|---------|
| `downloader.py` | Main script |
| `docker-compose.yml` | Docker configuration |
| `Dockerfile` | Container definition |
| `requirements.txt` | Python dependencies |
| `.jwt_cache.json` | Cached login token (auto-managed) |
| `stlflix.log` | Download log (written inside container, not persisted) |
| `<YOUR_DOWNLOAD_DIR>\.manifest.json` | Tracks all downloaded files |

## Troubleshooting

**Container exits immediately with "Set STLFLIX_EMAIL..."**
The env vars aren't reaching the container. Check `docker-compose.yml` has the `environment:` block.

**Download fails for specific files**
The script logs failures and continues. Check `docker logs` for lines with `FAILED` or `error`. Re-running will retry failed files.

**JWT expired**
The JWT lasts 30 days. Delete `.jwt_cache.json` to force a fresh login on next run.

**Want to re-download everything from scratch**
Delete `<YOUR_DOWNLOAD_DIR>\.manifest.json` and re-run. Existing files won't be re-downloaded (the script checks disk), but files that were deleted will be fetched again.
