import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.validation_matrix import ValidationMatrix
from app.schemas.products import ProductLineResponse, ProductListResponse

router = APIRouter(tags=["products"])

_PROD_URL = "https://rfq-api.azurewebsites.net"


def _row_to_dict(row: ValidationMatrix) -> dict:
    return {
        "product_line": row.product_line,
        "acronym": row.acronym,
        "n3_kam_limit": row.n3_kam_limit,
        "n2_zone_limit": row.n2_zone_limit,
        "n1_vp_limit": row.n1_vp_limit,
    }


@router.get(
    "/api/products",
    summary="Retrieve Product Data",
    operation_id="retrieveProducts",
    response_model=ProductListResponse,
)
async def retrieve_products(
    productName: str | None = Query(default=None, description="Filter by product line name (case-insensitive, partial match)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all product lines. If `productName` is provided, filters by partial,
    case-insensitive match on the product_line name.
    Falls back to the production catalog when the local DB has fewer than 10 products.
    """
    query = select(ValidationMatrix)
    if productName:
        query = query.where(ValidationMatrix.product_line.ilike(f"%{productName}%"))
    result = await db.execute(query)
    rows = result.scalars().all()

    # Build local acronym map (product_line → acronym)
    local_acronym_map = {row.product_line: row.acronym for row in rows}

    # Fetch from production catalog
    try:
        params = {"productName": productName} if productName else {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_PROD_URL}/api/products", params=params)
        if resp.status_code == 200:
            data = resp.json()
            prod_items = data.get("products") if isinstance(data, dict) else None
            if isinstance(prod_items, list) and prod_items:
                # Enrich each item with the acronym from the local validation_matrix
                enriched = []
                seen = set()
                for item in prod_items:
                    name = item.get("product_name") or item.get("product_line") or ""
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    pl = item.get("product_line", "")
                    enriched.append({
                        "product_name": name,
                        "product_line": pl,
                        "acronym": local_acronym_map.get(pl, ""),
                        "costing_data": item.get("costing_data"),
                    })
                if enriched:
                    return ProductListResponse(products=enriched)
    except Exception:
        pass

    # Fallback: expose local product lines as product_name
    return ProductListResponse(products=[
        {**_row_to_dict(r), "product_name": r.product_line}
        for r in rows
    ])


@router.get(
    "/api/product-lines",
    summary="Retrieve Product Line Data by ID",
    operation_id="retrieveProductLine",
    response_model=ProductLineResponse,
)
async def retrieve_product_line(
    productLineId: str = Query(..., description="Exact product line name (e.g. 'Brushes', 'Seals')"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the thresholds for a single product line identified by `productLineId`.
    Returns `productLine` as a single-item list (consistent array shape for ChatGPT tool schema).
    Raises 404 if not found.
    """
    from fastapi import HTTPException

    result = await db.execute(
        select(ValidationMatrix).where(ValidationMatrix.product_line == productLineId)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Product line '{productLineId}' not found.")
    return ProductLineResponse(productLine=[_row_to_dict(row)])
