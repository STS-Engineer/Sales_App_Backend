"""
SharePoint integration via Microsoft Graph API.

On RFQ approval, creates:
  {sharepoint_rfq_root_folder}/{product_line_folder}/{rfq_name}/
    01-Customer Input/
    02-Prototypes/
    03-Product design & Validation/
    04-Purchasing/
    05-Process/
    06-Costing/
    07-Project/

Pre-approval RFQ files are uploaded to 01-Customer Input.
The URL of the main RFQ folder (not 01-Customer Input) is stored in rfq.rfq_data["sharepoint"].

Auth: OAuth2 client_credentials (AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET).
All secrets come from environment variables — never hardcoded, never logged.
"""
import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from azure.storage.blob import BlobServiceClient
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import async_session_maker

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

PRODUCT_LINE_FOLDER_MAP: dict[str, str] = {
    "ASS": "Assembly",
    "BRU": "Brush",
    "CHO": "Choke",
    "FRI": "Friction",
    "SEA": "Seal",
}

RFQ_SUBFOLDERS: list[str] = [
    "01-Customer Input",
    "02-Prototypes",
    "03-Product design & Validation",
    "04-Purchasing",
    "05-Process",
    "06-Costing",
    "07-Project",
]

# In-process token cache
_token_cache: dict[str, Any] = {}


def encode_graph_path(path: str) -> str:
    """URL-encode a SharePoint path for Microsoft Graph API URLs.
    Encodes spaces, & and other special chars; preserves / separators.
    """
    return quote(path, safe="/")


def normalize_sharepoint_rfq_folder_name(rfq_name: str) -> str:
    """Strip the revision suffix '-00' from the RFQ name for SharePoint folder naming.

    26510-ASS-00 -> 26510-ASS
    26501-BRU-00 -> 26501-BRU
    """
    value = (rfq_name or "").strip()
    return re.sub(r"-00$", "", value)


def _raise_graph_error(resp: httpx.Response, context: str) -> None:
    """Log and re-raise Graph HTTP errors. Never logs credentials."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "Microsoft Graph error during [%s] | status=%s | response=%s",
            context,
            exc.response.status_code,
            exc.response.text[:1000],
        )
        raise


async def get_graph_token() -> str:
    """Obtain a Microsoft Graph access token via client credentials (with in-process cache)."""
    now = time.monotonic()
    if _token_cache.get("token") and _token_cache.get("expires_at", 0) > now + 60:
        return str(_token_cache["token"])

    tenant_id = settings.azure_tenant_id
    client_id = settings.azure_client_id
    client_secret = settings.azure_client_secret

    if not tenant_id or not client_id or not client_secret:
        raise ValueError(
            "SharePoint integration requires AZURE_TENANT_ID, AZURE_CLIENT_ID, and "
            "AZURE_CLIENT_SECRET to be set in environment variables."
        )

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        _raise_graph_error(resp, "get_graph_token")

    data = resp.json()
    token: str = data["access_token"]
    expires_in: int = int(data.get("expires_in", 3600))
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in
    return token


async def get_drive_id(token: str) -> str:
    """
    Resolve the SharePoint document library drive ID.

    Resolution order:
    1. SHAREPOINT_DRIVE_ID env var → skip all discovery
    2. SHAREPOINT_SITE_ID env var → skip group/site lookup, go straight to drives
    3. Full discovery: SHAREPOINT_GROUP_NAME → site → drives
    """
    configured_drive_id = settings.sharepoint_drive_id
    if configured_drive_id:
        logger.warning("DEBUG SHAREPOINT: using configured SHAREPOINT_DRIVE_ID=%s", configured_drive_id)
        return configured_drive_id

    library_name = settings.sharepoint_library_name
    headers = {"Authorization": f"Bearer {token}"}

    configured_site_id = settings.sharepoint_site_id
    if configured_site_id:
        site_id = configured_site_id
        logger.warning("DEBUG SHAREPOINT: using configured SHAREPOINT_SITE_ID=%s", site_id)
    else:
        group_name = settings.sharepoint_group_name
        logger.warning(
            "DEBUG SHAREPOINT: get_drive_id — group_name='%s' library_name='%s'",
            group_name,
            library_name,
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/groups",
                headers=headers,
                params={
                    "$filter": f"displayName eq '{group_name}'",
                    "$select": "id,displayName",
                },
            )
            _raise_graph_error(resp, f"list groups filter='{group_name}'")

        groups = resp.json().get("value", [])
        logger.warning(
            "DEBUG SHAREPOINT: groups matching '%s': count=%d",
            group_name,
            len(groups),
        )
        if not groups:
            raise ValueError(
                f"Microsoft 365 group '{group_name}' not found. "
                "Check SHAREPOINT_GROUP_NAME in your .env, or set SHAREPOINT_SITE_ID directly."
            )
        group_id: str = groups[0]["id"]
        logger.warning("DEBUG SHAREPOINT: group '%s' resolved — id=%s", group_name, group_id)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/groups/{group_id}/sites/root",
                headers=headers,
            )
            _raise_graph_error(resp, f"get site for group {group_id}")

        site_id = resp.json()["id"]
        logger.warning("DEBUG SHAREPOINT: site resolved — id=%s", site_id)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/sites/{site_id}/drives",
            headers=headers,
        )
        _raise_graph_error(resp, f"list drives for site {site_id}")

    drives = resp.json().get("value", [])
    available_names = [d.get("name") for d in drives]
    logger.warning(
        "DEBUG SHAREPOINT: available drives in site %s: %s",
        site_id,
        available_names,
    )

    drive = next((d for d in drives if d.get("name") == library_name), None)
    if drive is None:
        raise ValueError(
            f"Drive '{library_name}' not found in site {site_id}. "
            f"Available drives: {available_names}. "
            "Check SHAREPOINT_LIBRARY_NAME or set SHAREPOINT_DRIVE_ID directly."
        )

    drive_id: str = drive["id"]
    logger.warning("DEBUG SHAREPOINT: drive '%s' resolved — id=%s", library_name, drive_id)
    return drive_id


async def get_drive_item_by_path(token: str, drive_id: str, path: str) -> dict:
    """Fetch a drive item by path. Used to retrieve webUrl when folder already exists (409)."""
    encoded = encode_graph_path(path)
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{encoded}"
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)
        _raise_graph_error(resp, f"get drive item by path '{path}'")
    return dict(resp.json())


async def _create_folder(
    token: str,
    drive_id: str,
    parent_path: str,
    folder_name: str,
) -> dict:
    """
    Create a folder inside parent_path on the drive.
    Returns the drive item dict (created or existing).
    409 Conflict — fetches and returns the existing drive item (idempotent).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if parent_path:
        encoded_parent = encode_graph_path(parent_path)
        url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{encoded_parent}:/children"
    else:
        url = f"{GRAPH_BASE}/drives/{drive_id}/root/children"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers=headers,
            json={
                "name": folder_name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            },
        )

    if resp.status_code == 409:
        logger.debug("DEBUG SHAREPOINT: folder '%s/%s' already exists — fetching existing item", parent_path, folder_name)
        existing_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
        return await get_drive_item_by_path(token, drive_id, existing_path)

    _raise_graph_error(resp, f"create folder '{parent_path}/{folder_name}'")
    logger.warning("DEBUG SHAREPOINT: folder created — %s/%s", parent_path or "(root)", folder_name)
    return dict(resp.json())


async def ensure_rfq_folder_structure(
    token: str,
    drive_id: str,
    rfq_folder_name: str,
    product_line_folder: str,
) -> dict:
    """
    Create the full folder hierarchy under the RFQ root.

    Returns:
        {
            "rfq_folder_path": "RFQ/Assembly/26510-ASS",
            "customer_input_path": "RFQ/Assembly/26510-ASS/01-Customer Input",
            "rfq_folder_web_url": "https://...",
        }
    """
    root = settings.sharepoint_rfq_root_folder  # e.g. "RFQ"

    logger.warning(
        "DEBUG SHAREPOINT: ensure_rfq_folder_structure — root='%s' product_line_folder='%s' rfq_folder_name='%s'",
        root,
        product_line_folder,
        rfq_folder_name,
    )

    # RFQ/Assembly — product line folder (created if missing, drive item not needed)
    await _create_folder(token, drive_id, root, product_line_folder)
    product_line_path = f"{root}/{product_line_folder}"

    # RFQ/Assembly/26510-ASS — main RFQ folder, capture webUrl
    rfq_item = await _create_folder(token, drive_id, product_line_path, rfq_folder_name)
    rfq_path = f"{product_line_path}/{rfq_folder_name}"
    rfq_web_url: str = rfq_item.get("webUrl", "")

    logger.warning("DEBUG SHAREPOINT: rfq folder path resolved='%s'", rfq_path)
    if rfq_web_url:
        logger.warning("DEBUG SHAREPOINT: rfq folder webUrl resolved")
    else:
        logger.warning("DEBUG SHAREPOINT: rfq folder webUrl NOT found in Graph response")

    # 7 standard subfolders
    for subfolder in RFQ_SUBFOLDERS:
        await _create_folder(token, drive_id, rfq_path, subfolder)

    logger.warning("DEBUG SHAREPOINT: folder structure ready — %s", rfq_path)
    return {
        "rfq_folder_path": rfq_path,
        "customer_input_path": f"{rfq_path}/01-Customer Input",
        "rfq_folder_web_url": rfq_web_url,
    }


def _download_blob_sync(blob_name: str) -> bytes:
    """Download bytes from Azure Blob Storage (synchronous — run in thread pool)."""
    client = BlobServiceClient.from_connection_string(settings.azure_connection_string)
    blob = client.get_blob_client(
        container=settings.azure_rfq_files_container,
        blob=blob_name,
    )
    return blob.download_blob().readall()  # type: ignore[return-value]


async def upload_file_to_folder(
    token: str,
    drive_id: str,
    folder_path: str,
    file_name: str,
    file_bytes: bytes,
) -> None:
    """
    Upload a file to a SharePoint folder via Graph simple upload (≤4 MB).
    Replaces the file if it already exists.
    """
    encoded_folder = encode_graph_path(folder_path)
    encoded_name = quote(file_name, safe="")
    url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{encoded_folder}/{encoded_name}:/content"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.put(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            },
            content=file_bytes,
        )
        _raise_graph_error(resp, f"upload file '{file_name}' to '{folder_path}'")
    logger.warning("DEBUG SHAREPOINT: file uploaded — %s/%s", folder_path, file_name)


async def _update_rfq_sharepoint_data(rfq_id: str, folder_url: str, folder_path: str) -> None:
    """
    Persist sharepoint.folder_url and sharepoint.folder_path into rfq.rfq_data.
    Opens a fresh DB session — safe to call from a background task.
    Preserves all existing rfq_data fields.
    """
    from app.models.rfq import Rfq  # local import to avoid circular dependency at module load

    async with async_session_maker() as db:
        rfq = await db.get(Rfq, rfq_id)
        if rfq is None:
            logger.error("DEBUG SHAREPOINT: RFQ %s not found in DB — cannot update rfq_data", rfq_id)
            return

        existing_data = dict(rfq.rfq_data or {})
        existing_data["sharepoint"] = {
            "folder_url": folder_url or None,
            "folder_path": folder_path,
        }
        rfq.rfq_data = existing_data
        flag_modified(rfq, "rfq_data")
        await db.commit()
        logger.warning("DEBUG SHAREPOINT: rfq_data.sharepoint updated with SUCCESS for RFQ %s", rfq_id)


async def upload_feasibility_to_sharepoint(
    rfq_id: str,
    filename: str,
    file_bytes: bytes,
    file_label: str = "FEASIBILITY",
) -> None:
    """
    Upload a Costing Output file into RFQ/{ProductLine}/{RFQName}/08-Costing Output/.

    Used for both the Feasibility PDF (file_label="FEASIBILITY") and the Pricing Final Price
    file (file_label="PRICING"). Called as a FastAPI background task — never raises.
    """
    prefix = f"DEBUG SHAREPOINT {file_label}"
    logger.warning(
        "%s: rfq_id=%s filename='%s' size=%d bytes",
        prefix,
        rfq_id,
        filename,
        len(file_bytes),
    )
    try:
        if not settings.sharepoint_sync_enabled:
            logger.warning("%s: sync disabled — skipping for RFQ %s", prefix, rfq_id)
            return

        if not settings.azure_tenant_id or not settings.azure_client_id or not settings.azure_client_secret:
            logger.warning("%s: Azure credentials missing — skipping for RFQ %s", prefix, rfq_id)
            return

        from app.models.rfq import Rfq as RfqModel  # local import to avoid circular dep at module load

        rfq_folder_path: str | None = None
        rfq_name_raw: str = ""
        product_line_acronym: str = ""

        async with async_session_maker() as db:
            rfq = await db.get(RfqModel, rfq_id)
            if rfq is None:
                logger.error("%s: RFQ %s not found in DB", prefix, rfq_id)
                return
            sp_data = dict((rfq.rfq_data or {}).get("sharepoint") or {})
            rfq_folder_path = (sp_data.get("folder_path") or "").strip() or None
            rfq_name_raw = str((rfq.rfq_data or {}).get("systematic_rfq_id") or "")
            product_line_acronym = str(rfq.product_line_acronym or "")

        if rfq_folder_path:
            logger.warning(
                "%s: rfq_folder_path='%s' (from rfq_data.sharepoint.folder_path)",
                prefix,
                rfq_folder_path,
            )
        else:
            # Reconstruct from rfq_name and product_line when folder_path not yet stored
            product_line_key = product_line_acronym.strip().upper()
            product_line_folder = PRODUCT_LINE_FOLDER_MAP.get(product_line_key)
            if not rfq_name_raw or not product_line_folder:
                logger.error(
                    "%s: cannot resolve folder for RFQ %s — "
                    "no sharepoint.folder_path, rfq_name='%s', product_line='%s'",
                    prefix,
                    rfq_id,
                    rfq_name_raw,
                    product_line_acronym,
                )
                return
            sp_rfq_name = normalize_sharepoint_rfq_folder_name(rfq_name_raw)
            rfq_folder_path = f"{settings.sharepoint_rfq_root_folder}/{product_line_folder}/{sp_rfq_name}"
            logger.warning(
                "%s: no stored folder_path — reconstructed='%s'",
                prefix,
                rfq_folder_path,
            )

        logger.warning("%s: ensuring folder='%s/08-Costing Output'", prefix, rfq_folder_path)

        token = await get_graph_token()
        drive_id = await get_drive_id(token)

        # Create 08-Costing Output (idempotent — 409 Conflict handled in _create_folder)
        await _create_folder(token, drive_id, rfq_folder_path, "08-Costing Output")
        costing_output_path = f"{rfq_folder_path}/08-Costing Output"

        logger.warning("%s: uploading filename='%s'", prefix, filename)
        await upload_file_to_folder(token, drive_id, costing_output_path, filename, file_bytes)
        logger.warning("%s: upload success — '%s/%s'", prefix, costing_output_path, filename)

    except Exception as exc:
        logger.error(
            "ERROR SHAREPOINT %s: upload failed for RFQ %s: %s",
            file_label,
            rfq_id,
            exc,
            exc_info=True,
        )


async def sync_rfq_to_sharepoint(
    rfq_id: str,
    rfq_name: str,
    product_line_acronym: str,
    rfq_files: list[dict],
) -> None:
    """
    Create the SharePoint folder structure, upload pre-approval RFQ files,
    and store the RFQ folder URL in rfq.rfq_data["sharepoint"].

    Called as a FastAPI background task after successful RFQ approval.
    Never raises — logs errors so approval is never reversed.
    """
    logger.warning(
        "DEBUG SHAREPOINT: sync_rfq_to_sharepoint CALLED for RFQ %s — rfq_name='%s' product_line='%s' files_count=%d",
        rfq_id,
        rfq_name,
        product_line_acronym,
        len(rfq_files),
    )

    try:
        if not settings.sharepoint_sync_enabled:
            logger.warning("DEBUG SHAREPOINT: sync disabled (SHAREPOINT_SYNC_ENABLED=false) for RFQ %s", rfq_id)
            return

        if not settings.azure_tenant_id:
            logger.warning("DEBUG SHAREPOINT: sync skipped — AZURE_TENANT_ID not set in .env")
            return
        if not settings.azure_client_id:
            logger.warning("DEBUG SHAREPOINT: sync skipped — AZURE_CLIENT_ID not set in .env")
            return
        if not settings.azure_client_secret:
            logger.warning("DEBUG SHAREPOINT: sync skipped — AZURE_CLIENT_SECRET not set in .env")
            return

        if not rfq_name:
            logger.error("DEBUG SHAREPOINT: sync aborted — rfq_name (systematic_rfq_id) is empty for RFQ %s", rfq_id)
            return

        product_line_key = (product_line_acronym or "").strip().upper()
        product_line_folder = PRODUCT_LINE_FOLDER_MAP.get(product_line_key)

        logger.warning(
            "DEBUG SHAREPOINT: mapping — product_line_acronym='%s' -> key='%s' -> folder='%s'",
            product_line_acronym,
            product_line_key,
            product_line_folder,
        )

        if not product_line_folder:
            logger.error(
                "DEBUG SHAREPOINT: sync aborted — product_line_key '%s' not in PRODUCT_LINE_FOLDER_MAP %s for RFQ %s",
                product_line_key,
                list(PRODUCT_LINE_FOLDER_MAP.keys()),
                rfq_id,
            )
            return

        # Guard: if the folder is already recorded in the DB, skip creation entirely.
        from app.models.rfq import Rfq  # local import — avoids circular dependency
        async with async_session_maker() as _db:
            _rfq = await _db.get(Rfq, rfq_id)
            _existing_path = (
                (_rfq.rfq_data or {}).get("sharepoint", {}).get("folder_path")
                if _rfq else None
            )
        if _existing_path:
            logger.warning(
                "DEBUG SHAREPOINT: folder already exists in DB for RFQ %s ('%s') — skipping creation",
                rfq_id,
                _existing_path,
            )
            return

        sharepoint_rfq_folder_name = normalize_sharepoint_rfq_folder_name(rfq_name)
        logger.warning(
            "DEBUG SHAREPOINT: rfq_name original='%s' sharepoint_folder_name='%s'",
            rfq_name,
            sharepoint_rfq_folder_name,
        )

        token = await get_graph_token()
        drive_id = await get_drive_id(token)

        result = await ensure_rfq_folder_structure(
            token, drive_id, sharepoint_rfq_folder_name, product_line_folder
        )

        # Store the main RFQ folder URL (not 01-Customer Input) in rfq_data
        await _update_rfq_sharepoint_data(
            rfq_id=rfq_id,
            folder_url=result["rfq_folder_web_url"],
            folder_path=result["rfq_folder_path"],
        )

        # Upload each pre-approval file to 01-Customer Input
        customer_input_path = result["customer_input_path"]
        for file_meta in rfq_files:
            blob_name: str = (file_meta.get("blob_name") or "").strip()
            file_name: str = (
                file_meta.get("filename")
                or file_meta.get("name")
                or (blob_name.split("/")[-1] if blob_name else "")
            ).strip()

            if not blob_name or not file_name:
                logger.warning(
                    "DEBUG SHAREPOINT: skipping file with missing blob_name/filename for RFQ %s: %s",
                    rfq_id,
                    file_meta,
                )
                continue

            try:
                file_bytes = await asyncio.to_thread(_download_blob_sync, blob_name)
                await upload_file_to_folder(token, drive_id, customer_input_path, file_name, file_bytes)
            except Exception as file_exc:
                logger.error(
                    "DEBUG SHAREPOINT: failed to upload file '%s' for RFQ %s: %s",
                    file_name,
                    rfq_id,
                    file_exc,
                )

        logger.warning("DEBUG SHAREPOINT: sync_rfq_to_sharepoint COMPLETED for RFQ %s", rfq_id)

    except Exception as exc:
        # Never propagate from a background task — Starlette would crash the ASGI worker.
        logger.error(
            "DEBUG SHAREPOINT: sync_rfq_to_sharepoint FAILED for RFQ %s: %s",
            rfq_id,
            exc,
            exc_info=True,
        )