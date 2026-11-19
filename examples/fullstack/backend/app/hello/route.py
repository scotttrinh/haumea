def GET(name: str = "world"):
    """Greet someone by name."""
    return {"hello": name}
