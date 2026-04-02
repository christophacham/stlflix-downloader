#!/usr/bin/env python3
"""
STLFlix Downloader — bulk download, sync, verify.

Commands
--------
  (no args)      Download / sync everything missing
  --dry-run      Show what would be downloaded, no changes
  --verify       Check every local file against manifest, report missing
  --status       One-line summary of local completeness (no API needed)
"""

import os
import sys
import json
import time
import base64
import logging
import asyncio
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import aiofiles
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL         = "https://k8s.stlflix.com"
LOGIN_URL        = f"{BASE_URL}/api/auth/local"
GRAPHQL_URL      = f"{BASE_URL}/graphql"
PRODUCT_FILE_URL = f"{BASE_URL}/api/product/product-file"

DOWNLOAD_DIR    = Path(os.getenv("DOWNLOAD_DIR", r"D:\3dPrint\STLFLIX"))
JWT_CACHE_FILE  = Path(__file__).parent / ".jwt_cache.json"
MANIFEST_FILE   = DOWNLOAD_DIR / ".manifest.json"
CATALOGUE_FILE  = DOWNLOAD_DIR / ".catalogue.json"
LOG_FILE        = Path(__file__).parent / "stlflix.log"

MAX_CONCURRENT  = 5
PAGE_SIZE       = 20

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Manifest helpers ──────────────────────────────────────────────────────────
#
# Manifest structure (nested, human-readable):
#
# {
#   "last_sync": "2026-04-01T...",
#   "drops": {
#     "drop-315": {
#       "title": "Drop #315",
#       "release_date": "2026-04-01",
#       "products": {
#         "skull-sphere-table-lamp": {
#           "name": "Skull Sphere - Table Lamp",
#           "files": {
#             "thumbnail": {"status": "ok", "path": "drop-315/.../preview.png"},
#             "PDF":       {"status": "ok", "path": "drop-315/.../Foo.pdf"},
#             "FILES":     {"status": "ok", "path": "drop-315/.../Foo.zip"},
#           }
#         }
#       }
#     }
#   }
# }

def load_manifest() -> dict:
    if MANIFEST_FILE.exists():
        try:
            return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"drops": {}}


def save_manifest(manifest: dict):
    manifest["last_sync"] = datetime.now(tz=timezone.utc).isoformat()
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _product_entry(manifest: dict, drop_slug: str, product_slug: str) -> dict:
    """Return (and create if needed) the product entry in the manifest."""
    manifest.setdefault("drops", {})
    manifest["drops"].setdefault(drop_slug, {"products": {}})
    manifest["drops"][drop_slug]["products"].setdefault(product_slug, {"files": {}})
    return manifest["drops"][drop_slug]["products"][product_slug]


def _set_file_status(manifest: dict, drop_slug: str, product_slug: str,
                     key: str, status: str, path: Optional[str]):
    entry = _product_entry(manifest, drop_slug, product_slug)
    entry["files"][key] = {
        "status": status,
        "path":   path,
        "ts":     datetime.now(tz=timezone.utc).isoformat(),
    }


def _get_file_status(manifest: dict, drop_slug: str, product_slug: str, key: str) -> dict:
    return (
        manifest.get("drops", {})
                .get(drop_slug, {})
                .get("products", {})
                .get(product_slug, {})
                .get("files", {})
                .get(key, {})
    )

# ── JWT / Auth ────────────────────────────────────────────────────────────────

def _decode_jwt_exp(jwt: str) -> int:
    try:
        padding = "=" * (4 - len(jwt.split(".")[1]) % 4)
        return json.loads(base64.b64decode(jwt.split(".")[1] + padding)).get("exp", 0)
    except Exception:
        return 0


def load_cached_jwt() -> Optional[str]:
    if JWT_CACHE_FILE.exists():
        try:
            data = json.loads(JWT_CACHE_FILE.read_text())
            if data.get("exp", 0) > time.time() + 3600:
                return data.get("jwt", "")
        except Exception:
            pass
    return None


def cache_jwt(jwt: str):
    JWT_CACHE_FILE.write_text(
        json.dumps({"jwt": jwt, "exp": _decode_jwt_exp(jwt)}), encoding="utf-8"
    )


async def login(session: aiohttp.ClientSession) -> str:
    cached = load_cached_jwt()
    if cached:
        log.info("Using cached JWT (still valid)")
        return cached

    email    = os.getenv("STLFLIX_EMAIL")
    password = os.getenv("STLFLIX_PASSWORD")
    if not email or not password:
        raise SystemExit("Set STLFLIX_EMAIL and STLFLIX_PASSWORD in .env or environment")

    log.info("Authenticating with STLFlix...")
    async with session.post(LOGIN_URL, json={"identifier": email, "password": password}) as resp:
        data = await resp.json()

    if "jwt" not in data:
        raise SystemExit(f"Login failed: {data.get('error', {}).get('message', data)}")

    jwt = data["jwt"]
    cache_jwt(jwt)
    exp_dt = datetime.fromtimestamp(_decode_jwt_exp(jwt), tz=timezone.utc)
    log.info(f"Logged in (user {data['user']['id']}, JWT expires {exp_dt:%Y-%m-%d})")
    return jwt

# ── GraphQL ───────────────────────────────────────────────────────────────────

_DROPS_QUERY = """
query GetDrops($page: Int!, $pageSize: Int!, $now: DateTime!) {
  drops(
    filters: { release_date: { lte: $now } }
    pagination: { page: $page, pageSize: $pageSize }
    sort: "release_date:desc"
  ) {
    data {
      id
      attributes {
        title
        slug
        release_date
        products {
          data {
            id
            attributes {
              name
              slug
              thumbnail {
                data { attributes { url } }
              }
              files {
                text
                commercial_only
                file_url
                file { data { id } }
              }
            }
          }
        }
      }
    }
    meta { pagination { page pageCount total } }
  }
}
"""


async def fetch_all_drops(session: aiohttp.ClientSession, jwt: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    now     = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    drops   = []
    page    = 1

    while True:
        log.info(f"  Fetching drops page {page}...")
        async with session.post(
            GRAPHQL_URL,
            headers=headers,
            json={"query": _DROPS_QUERY, "variables": {"page": page, "pageSize": PAGE_SIZE, "now": now}},
        ) as resp:
            body = await resp.json()

        if "errors" in body:
            raise RuntimeError(f"GraphQL error: {body['errors']}")

        chunk      = body["data"]["drops"]
        drops     += chunk["data"]
        pagination = chunk["meta"]["pagination"]
        log.info(
            f"    page {pagination['page']}/{pagination['pageCount']} "
            f"({len(chunk['data'])} drops, {pagination['total']} total)"
        )
        if page >= pagination["pageCount"]:
            break
        page += 1

    return drops


def save_catalogue(all_drops: list[dict]):
    """Write a human-readable catalogue of everything STLFlix has."""
    catalogue = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_drops":    len(all_drops),
        "total_products": sum(len(d["attributes"]["products"]["data"]) for d in all_drops),
        "drops": [
            {
                "id":           d["id"],
                "title":        d["attributes"]["title"],
                "slug":         d["attributes"]["slug"],
                "release_date": d["attributes"]["release_date"][:10],
                "products": [
                    {
                        "id":    p["id"],
                        "name":  p["attributes"]["name"],
                        "slug":  p["attributes"]["slug"],
                        "files": [
                            {
                                "type":            f.get("text"),
                                "file_id":         (f.get("file") or {}).get("data", {}).get("id"),
                                "commercial_only": f.get("commercial_only", False),
                            }
                            for f in (p["attributes"].get("files") or [])
                            if f.get("file", {}) and f["file"].get("data")
                        ],
                    }
                    for p in d["attributes"]["products"]["data"]
                ],
            }
            for d in all_drops
        ],
    }
    CATALOGUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATALOGUE_FILE.write_text(json.dumps(catalogue, indent=2), encoding="utf-8")
    log.info(
        f"Catalogue saved → {CATALOGUE_FILE}  "
        f"({catalogue['total_drops']} drops, {catalogue['total_products']} products)"
    )

# ── Download helpers ──────────────────────────────────────────────────────────

async def resolve_file_url(
    session: aiohttp.ClientSession, jwt: str, file_id: str
) -> Optional[dict]:
    headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}
    try:
        async with session.post(
            PRODUCT_FILE_URL, headers=headers, json={"fid": str(file_id)}
        ) as resp:
            data = await resp.json()
        if "url" in data:
            return data
    except Exception as e:
        log.warning(f"    product-file API error fid={file_id}: {e}")
    return None


async def download_file(session: aiohttp.ClientSession, url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                log.error(f"    HTTP {resp.status} — {url}")
                return False
            async with aiofiles.open(tmp, "wb") as fh:
                async for chunk in resp.content.iter_chunked(65536):
                    await fh.write(chunk)
        tmp.rename(dest)
        return True
    except Exception as e:
        log.error(f"    Download error: {e}")
        if tmp.exists():
            tmp.unlink()
        return False

# ── Per-product processing ────────────────────────────────────────────────────

async def process_product(
    session:      aiohttp.ClientSession,
    jwt:          str,
    drop_slug:    str,
    product:      dict,
    manifest:     dict,
    sem:          asyncio.Semaphore,
    dry_run:      bool,
) -> tuple[int, int]:
    attrs        = product["attributes"]
    product_slug = attrs["slug"]
    files        = attrs.get("files") or []
    thumb_data   = (attrs.get("thumbnail") or {}).get("data") or {}
    thumb_url    = (thumb_data.get("attributes") or {}).get("url")

    # Annotate manifest with drop/product metadata once
    drop_entry = manifest.setdefault("drops", {}).setdefault(drop_slug, {"products": {}})
    prod_entry = drop_entry["products"].setdefault(product_slug, {"files": {}})
    prod_entry["name"] = attrs["name"]

    downloaded = 0
    skipped    = 0

    # ── Thumbnail ─────────────────────────────────────────────────────────────
    if thumb_url:
        ext        = Path(thumb_url.split("?")[0]).suffix or ".png"
        thumb_dest = DOWNLOAD_DIR / drop_slug / product_slug / f"preview{ext}"
        info       = _get_file_status(manifest, drop_slug, product_slug, "thumbnail")

        if info.get("status") == "ok" and thumb_dest.exists():
            skipped += 1
        else:
            if dry_run:
                log.info(f"  DRY-RUN {drop_slug}/{product_slug}/preview{ext} [thumbnail]")
            else:
                ok = await download_file(session, thumb_url, thumb_dest)
                _set_file_status(manifest, drop_slug, product_slug, "thumbnail",
                                 "ok" if ok else "failed",
                                 str(thumb_dest.relative_to(DOWNLOAD_DIR)) if ok else None)
                if ok:
                    downloaded += 1

    # ── Print files ───────────────────────────────────────────────────────────
    for entry in files:
        file_type  = entry.get("text", "?")
        direct_url = entry.get("file_url")
        file_id    = (entry.get("file") or {}).get("data", {}).get("id") if not direct_url else None

        if not file_id and not direct_url:
            continue

        key  = f"{file_type}/{file_id or direct_url}"
        info = _get_file_status(manifest, drop_slug, product_slug, key)

        if info.get("status") == "ok":
            local = DOWNLOAD_DIR / info["path"] if info.get("path") else None
            if local and local.exists():
                skipped += 1
                continue

        async with sem:
            if direct_url:
                s3_url   = direct_url
                filename = direct_url.rstrip("/").split("/")[-1]
            else:
                file_info = await resolve_file_url(session, jwt, file_id)
                if not file_info:
                    _set_file_status(manifest, drop_slug, product_slug, key, "api_error", None)
                    continue
                s3_url   = file_info["url"]
                filename = file_info["name"]

            dest = DOWNLOAD_DIR / drop_slug / product_slug / filename

            if dest.exists():
                _set_file_status(manifest, drop_slug, product_slug, key, "ok",
                                 str(dest.relative_to(DOWNLOAD_DIR)))
                skipped += 1
                continue

            if dry_run:
                log.info(f"  DRY-RUN {drop_slug}/{product_slug}/{filename} [{file_type}]")
                continue

            log.info(f"  ↓ {drop_slug}/{product_slug}/{filename} [{file_type}]")
            ok = await download_file(session, s3_url, dest)
            _set_file_status(manifest, drop_slug, product_slug, key,
                             "ok" if ok else "failed",
                             str(dest.relative_to(DOWNLOAD_DIR)) if ok else None)
            if ok:
                downloaded += 1

    return downloaded, skipped

# ── Index generation ──────────────────────────────────────────────────────────

def generate_index(all_drops: list[dict]):
    index_path = DOWNLOAD_DIR / "index.html"

    drop_blocks = []
    for drop in all_drops:
        attrs    = drop["attributes"]
        slug     = attrs["slug"]
        title    = attrs["title"]
        date     = attrs["release_date"][:10]
        products = attrs["products"]["data"]

        product_cards = []
        for p in products:
            pa    = p["attributes"]
            pslug = pa["slug"]
            pname = pa["name"]

            thumb_data = (pa.get("thumbnail") or {}).get("data") or {}
            thumb_url  = (thumb_data.get("attributes") or {}).get("url", "")
            ext        = Path(thumb_url.split("?")[0]).suffix if thumb_url else ".png"
            preview    = f"{slug}/{pslug}/preview{ext}"
            local_img  = DOWNLOAD_DIR / slug / pslug / f"preview{ext}"

            file_labels = [e.get("text", "") for e in (pa.get("files") or []) if e.get("text")]
            tooltip = " · ".join(file_labels)

            img_html = (
                f'<img src="{preview}" alt="{pname}" loading="lazy" onerror="this.style.display=\'none\'">'
                if local_img.exists() else
                '<div class="no-img">?</div>'
            )
            product_cards.append(f"""
          <div class="product" title="{tooltip}">
            {img_html}
            <span>{pname}</span>
          </div>""")

        drop_blocks.append(f"""
    <section class="drop">
      <div class="drop-header">
        <h2>{title}</h2>
        <span class="meta">{date} &nbsp;·&nbsp; {len(products)} model{"s" if len(products) != 1 else ""}</span>
      </div>
      <div class="products">{"".join(product_cards)}
      </div>
    </section>""")

    total_drops    = len(all_drops)
    total_products = sum(len(d["attributes"]["products"]["data"]) for d in all_drops)
    generated_at   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>STLFlix Collection</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #111318; color: #e8eaf0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; line-height: 1.5; }}
    header {{ position: sticky; top: 0; z-index: 10; background: #1a1d26; border-bottom: 1px solid #2a2d3a; padding: 14px 24px; display: flex; align-items: baseline; gap: 16px; }}
    header h1 {{ font-size: 18px; font-weight: 700; color: #fff; }}
    header .stats {{ font-size: 12px; color: #7c8099; }}
    header .generated {{ margin-left: auto; font-size: 11px; color: #4a4e62; }}
    .drops {{ max-width: 1600px; margin: 0 auto; padding: 24px 20px 60px; display: flex; flex-direction: column; gap: 32px; }}
    .drop {{ background: #1a1d26; border: 1px solid #242736; border-radius: 10px; overflow: hidden; }}
    .drop-header {{ display: flex; align-items: baseline; gap: 12px; padding: 14px 18px; border-bottom: 1px solid #242736; background: #1e2130; }}
    .drop-header h2 {{ font-size: 15px; font-weight: 600; color: #fff; }}
    .drop-header .meta {{ font-size: 12px; color: #7c8099; }}
    .products {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 1px; background: #242736; }}
    .product {{ background: #1a1d26; display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 12px 8px; transition: background .15s; }}
    .product:hover {{ background: #22253a; }}
    .product img {{ width: 100%; aspect-ratio: 1/1; object-fit: cover; border-radius: 6px; }}
    .product .no-img {{ width: 100%; aspect-ratio: 1/1; border-radius: 6px; background: #242736; display: flex; align-items: center; justify-content: center; font-size: 22px; color: #3a3e52; }}
    .product span {{ font-size: 11px; color: #9da3b8; text-align: center; line-height: 1.3; }}
  </style>
</head>
<body>
  <header>
    <h1>STLFlix Collection</h1>
    <span class="stats">{total_drops} drops &nbsp;·&nbsp; {total_products} models</span>
    <span class="generated">Generated {generated_at}</span>
  </header>
  <div class="drops">{"".join(drop_blocks)}
  </div>
</body>
</html>"""

    index_path.write_text(html, encoding="utf-8")
    log.info(f"index.html written → {index_path}")

# ── Verify command ────────────────────────────────────────────────────────────

def cmd_verify():
    """Check every manifest entry against disk. No network needed."""
    if not MANIFEST_FILE.exists():
        print("No manifest found. Run the downloader first.")
        sys.exit(1)

    manifest = load_manifest()
    drops    = manifest.get("drops", {})

    ok_count      = 0
    missing       = []
    failed        = []
    api_errors    = []

    for drop_slug, drop_data in sorted(drops.items()):
        for product_slug, product_data in sorted(drop_data.get("products", {}).items()):
            name  = product_data.get("name", product_slug)
            files = product_data.get("files", {})

            for key, info in files.items():
                status = info.get("status")
                path   = info.get("path")
                label  = f"{drop_slug}/{product_slug}  [{key}]"

                if status == "ok" and path:
                    full = DOWNLOAD_DIR / path
                    if full.exists():
                        ok_count += 1
                    else:
                        missing.append(label)
                elif status == "failed":
                    failed.append(label)
                elif status == "api_error":
                    api_errors.append(label)
                else:
                    missing.append(label)

    total = ok_count + len(missing) + len(failed) + len(api_errors)

    print(f"\nSTLFlix Local Verification")
    print(f"{'─' * 50}")
    print(f"  Manifest:  {MANIFEST_FILE}")
    print(f"  Checked:   {total} entries across {len(drops)} drops")
    print()
    print(f"  ✓  {ok_count:>6,}  present on disk")
    print(f"  ✗  {len(missing):>6,}  missing from disk")
    print(f"  !  {len(failed):>6,}  download failed last run")
    print(f"  ?  {len(api_errors):>6,}  API errors (no URL returned)")
    print(f"{'─' * 50}")
    pct = (ok_count / total * 100) if total else 0
    print(f"  {pct:.1f}% complete\n")

    if missing:
        print(f"Missing files ({len(missing)}):")
        for m in missing[:50]:
            print(f"    {m}")
        if len(missing) > 50:
            print(f"    … and {len(missing) - 50} more")
        print()

    if failed:
        print(f"Failed downloads ({len(failed)}) — re-run without --verify to retry:")
        for f in failed[:20]:
            print(f"    {f}")
        if len(failed) > 20:
            print(f"    … and {len(failed) - 20} more")
        print()

    sys.exit(0 if not missing and not failed else 1)


def cmd_status():
    """Print a quick summary from the manifest. No network needed."""
    if not MANIFEST_FILE.exists():
        print("No manifest found. Run the downloader first.")
        sys.exit(1)

    manifest = load_manifest()
    drops    = manifest.get("drops", {})

    ok = failed = errors = missing = 0
    for drop_data in drops.values():
        for product_data in drop_data.get("products", {}).values():
            for info in product_data.get("files", {}).values():
                s = info.get("status", "")
                if s == "ok":
                    ok += 1
                elif s == "failed":
                    failed += 1
                elif s == "api_error":
                    errors += 1
                else:
                    missing += 1

    total = ok + failed + errors + missing
    pct   = (ok / total * 100) if total else 0
    last  = manifest.get("last_sync", "never")[:19].replace("T", " ")

    print(f"\nSTLFlix  |  {len(drops)} drops  |  {ok:,}/{total:,} files ({pct:.1f}%)  |  last sync {last}")
    if failed:  print(f"           {failed} failed downloads — re-run to retry")
    if errors:  print(f"           {errors} API errors")
    print()


# ── Main runner ───────────────────────────────────────────────────────────────

async def run(dry_run: bool = False):
    manifest   = load_manifest()
    sem        = asyncio.Semaphore(MAX_CONCURRENT)
    connector  = aiohttp.TCPConnector(limit=20)
    timeout    = aiohttp.ClientTimeout(total=600, connect=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        jwt = await login(session)

        log.info("Fetching complete drop catalogue from GraphQL...")
        all_drops = await fetch_all_drops(session, jwt)
        log.info(f"Total drops: {len(all_drops)}")

        # Save catalogue (newest-first, as returned by API)
        save_catalogue(all_drops)

        # Annotate manifest with drop titles
        for drop in all_drops:
            attrs = drop["attributes"]
            entry = manifest.setdefault("drops", {}).setdefault(attrs["slug"], {"products": {}})
            entry["title"]        = attrs["title"]
            entry["release_date"] = attrs["release_date"][:10]

        # Download oldest-first
        drops_for_download = sorted(all_drops, key=lambda d: d["attributes"]["release_date"])

        total_dl = total_sk = total_err = 0

        for drop in drops_for_download:
            attrs    = drop["attributes"]
            slug     = attrs["slug"]
            products = attrs["products"]["data"]
            if not products:
                continue

            log.info(f"\n── {attrs['title']} ({slug})  [{len(products)} products]")

            tasks = [
                process_product(session, jwt, slug, p, manifest, sem, dry_run)
                for p in products
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for r in results:
                if isinstance(r, tuple):
                    total_dl += r[0]
                    total_sk += r[1]
                elif isinstance(r, Exception):
                    log.error(f"  Product error: {r}")
                    total_err += 1

            save_manifest(manifest)

        save_manifest(manifest)

        if not dry_run:
            log.info("\nGenerating index.html...")
            generate_index(all_drops)

        log.info(
            f"\n{'DRY-RUN ' if dry_run else ''}Finished — "
            f"downloaded: {total_dl}  already present: {total_sk}  errors: {total_err}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="STLFlix downloader / sync / verify",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
commands:
  (no args)   Download / sync all missing files
  --dry-run   Show what would be downloaded, no changes
  --verify    Check every manifest entry against disk (no network)
  --status    One-line summary from manifest (no network)
        """,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="List missing files without downloading")
    parser.add_argument("--verify",  action="store_true",
                        help="Verify local files against manifest")
    parser.add_argument("--status",  action="store_true",
                        help="Quick summary from manifest")
    args = parser.parse_args()

    if args.verify:
        cmd_verify()
    elif args.status:
        cmd_status()
    else:
        asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
