from pydantic import BaseModel


class Item(BaseModel):
    name: str
    price: float


def GET(item_id: int):
    return {"item_id": item_id}


async def PUT(item_id: int, item: Item):
    return {"item_id": item_id, **item.model_dump()}
