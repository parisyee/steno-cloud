"""Search endpoints — GET /search."""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from api.deps import check_auth, get_supabase

router = APIRouter()


@router.get("/search")
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=20, le=100),
    _: None = Depends(check_auth),
):
    rows = (
        get_supabase()
        .table("transcriptions")
        .select("id, filename, text, created_at")
        .text_search("search_vec", q)
        .execute()
    )
    return JSONResponse({"results": rows.data[:limit]})
