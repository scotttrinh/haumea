from typing import Annotated

from msgspec import Meta, Struct

from haumea import Depends, Header, HTTPException

# Per-function settings, like Next.js route segment config.
max_duration = 10


class Item(Struct):
    name: str
    price: Annotated[float, Meta(gt=0)]


def current_user(authorization: str = Header()) -> str:
    if authorization != "Bearer secret":
        raise HTTPException(401, "Not authenticated")
    return "scott"


def GET(item_id: int, verbose: bool = False):
    """Fetch an item."""
    return {"item_id": item_id, "verbose": verbose}


async def PUT(item_id: int, item: Item, user: str = Depends(current_user)) -> Item:
    """Replace an item. Requires `Authorization: Bearer secret`."""
    return item
