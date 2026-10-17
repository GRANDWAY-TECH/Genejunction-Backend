from collections.abc import Callable
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class PageParams(BaseModel):
    limit: int = 20
    offset: int = 0


def _page_params(
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000)
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageDep = Annotated[PageParams, Depends(_page_params)]


async def paginate(
    session: AsyncSession, stmt: Select, params: PageParams, mapper: Callable[[Any], Any] | None = None
) -> dict:
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.scalars(stmt.limit(params.limit).offset(params.offset))).all()
    items = [mapper(r) for r in rows] if mapper else list(rows)
    return {"items": items, "total": total or 0, "limit": params.limit, "offset": params.offset}
