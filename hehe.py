# hehe.py — Packsmith's first Monaco test file

import random


def generate_loot_table(item_ids: list[str], weight: int = 1) -> dict:
    """Generate a basic Minecraft loot table from a list of item IDs."""
    entries = []
    for item_id in item_ids:
        entries.append({
            "type": "minecraft:item",
            "name": item_id,
            "weight": weight,
        })

    return {
        "type": "minecraft:block",
        "pools": [{
            "rolls": 1,
            "entries": entries,
        }]
    }

#I wish there were such a thing as a flounder... ya know???? OH MY FUCKING GOD!!! WAIT.... HTERE IS SUCH THING AS A FLOUNDER HOLY SHIT!!!

# i like beans... 
def roll_loot(table: dict, times: int = 1) -> list[str]:
    """Simulate rolling a loot table N times."""
    pool = table["pools"][0]
    entries = pool["entries"]
    weights = [e["weight"] for e in entries]
    names = [e["name"] for e in entries]
    return random.choices(names, weights=weights, k=times)


if __name__ == "__main__":
    items = [
        "minecraft:diamond_sword",
        "minecraft:golden_apple",
        "minecraft:netherite_ingot",
        "alexscaves:abyssmarine_chestplate",
    ]
    table = generate_loot_table(items, weight=10)
    results = roll_loot(table, times=20)
    for item in results:
        print(f"  -> {item}")