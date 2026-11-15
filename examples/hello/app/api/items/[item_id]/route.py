from pydantic import BaseModel

from haumea import Depends, Header, HTTPException

# Per-function settings, like Next.js route segment config.
max_duration = 10


class Item(BaseModel):
    name: str
    price: float


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
