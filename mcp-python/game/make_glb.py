"""Write a minimal binary glTF (.glb) arena: floor + 3 crates + a player block.

No dependencies. Geometry is built at the origin and placed with node translations,
which is what a real exporter does - and what a game needs, because gameplay code
reads node positions.
"""
import json, struct, pathlib

def box(sx, sy, sz):
    """A box centred on the origin. Returns (vertices, indices)."""
    x, y, z = sx / 2, sy / 2, sz / 2
    v = [(-x,-y,-z),(x,-y,-z),(x,y,-z),(-x,y,-z),(-x,-y,z),(x,-y,z),(x,y,z),(-x,y,z)]
    idx = [0,1,2, 0,2,3, 4,6,5, 4,7,6, 0,4,5, 0,5,1,
           3,2,6, 3,6,7, 0,3,7, 0,7,4, 1,5,6, 1,6,2]
    return v, idx


# name, size, material index, node translation
PARTS = [
    ("Floor",  (24, 0.5, 24),   0, [0.0, -0.25, 0.0]),
    ("Crate1", (1.5, 1.5, 1.5), 1, [-4.0, 0.75, -3.0]),
    ("Crate2", (1.5, 1.5, 1.5), 1, [3.5, 0.75, 2.0]),
    ("Crate3", (1.5, 1.5, 1.5), 1, [0.0, 0.75, 6.0]),
    ("Player", (0.8, 1.8, 0.8), 2, [0.0, 0.9, 0.0]),
]

buf = bytearray()
accessors, buffer_views, meshes, nodes = [], [], [], []

for i, (name, size, material, translation) in enumerate(PARTS):
    verts, idx = box(*size)

    off = len(buf)
    for n in idx:
        buf += struct.pack("<H", n)
    while len(buf) % 4:
        buf += b"\0"
    buffer_views.append({"buffer": 0, "byteOffset": off, "byteLength": len(idx) * 2})
    accessors.append({"bufferView": len(buffer_views) - 1, "componentType": 5123,
                      "count": len(idx), "type": "SCALAR"})
    idx_acc = len(accessors) - 1

    off = len(buf)
    for vx, vy, vz in verts:
        buf += struct.pack("<fff", vx, vy, vz)
    buffer_views.append({"buffer": 0, "byteOffset": off, "byteLength": len(verts) * 12})
    accessors.append({"bufferView": len(buffer_views) - 1, "componentType": 5126,
                      "count": len(verts), "type": "VEC3",
                      "min": [min(v[k] for v in verts) for k in range(3)],
                      "max": [max(v[k] for v in verts) for k in range(3)]})
    pos_acc = len(accessors) - 1

    meshes.append({"name": name, "primitives": [
        {"attributes": {"POSITION": pos_acc}, "indices": idx_acc, "material": material}]})
    nodes.append({"name": name, "mesh": i, "translation": translation})

gltf = {
    "asset": {"version": "2.0", "generator": "workshop make_glb.py"},
    "scene": 0,
    "scenes": [{"name": "Arena", "nodes": list(range(len(nodes)))}],
    "nodes": nodes,
    "meshes": meshes,
    "materials": [
        {"name": "Ground", "pbrMetallicRoughness": {
            "baseColorFactor": [0.22, 0.24, 0.28, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.95}},
        {"name": "Crate", "pbrMetallicRoughness": {
            "baseColorFactor": [0.76, 0.52, 0.24, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.7}},
        {"name": "PlayerSkin", "pbrMetallicRoughness": {
            "baseColorFactor": [0.29, 0.87, 0.5, 1.0], "metallicFactor": 0.1, "roughnessFactor": 0.4}},
    ],
    "accessors": accessors,
    "bufferViews": buffer_views,
    "buffers": [{"byteLength": len(buf)}],
}

json_chunk = json.dumps(gltf, separators=(",", ":")).encode()
json_chunk += b" " * ((4 - len(json_chunk) % 4) % 4)
bin_chunk = bytes(buf) + b"\0" * ((4 - len(buf) % 4) % 4)

glb = b"glTF" + struct.pack("<II", 2, 12 + 8 + len(json_chunk) + 8 + len(bin_chunk))
glb += struct.pack("<I", len(json_chunk)) + b"JSON" + json_chunk
glb += struct.pack("<I", len(bin_chunk)) + b"BIN\0" + bin_chunk

out = pathlib.Path(__file__).parent / "assets" / "arena.glb"
out.write_bytes(glb)
print(f"wrote {out} ({len(glb)} bytes, {len(PARTS) * 12} triangles)")
