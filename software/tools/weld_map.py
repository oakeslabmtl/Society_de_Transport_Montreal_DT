"""Welds the city map's vertices: the source GLB gives almost every triangle its own three vertices (a flat-shaded
"triangle soup"), so the engine cannot simplify it into levels of detail and draws 4.6 times more vertices than needed.

Vertices with bit-identical positions become one (the geometry does not change), triangles keep their order and
winding, nodes keep their names, hierarchy and transforms (the import settings are keyed by node name). Normals and UVs
are dropped: the map's shader computes flat normals itself, and there is no texture. Every primitive gets the material
"map" (mapped to the flat shader in the .import file).

It made visualization/assets/map not joined.glb (74.6 MB) from the original (535 MB, in git history). Run it on that
original only. From the repository root, in Git Bash (in Windows PowerShell 5.1, `>` re-encodes the output as text
and corrupts the GLB), the original into a separate file (the tracked map and the index stay as they are), then the
welding (numpy: the project's .venv has it):
    git cat-file --filters c9ee58a:"visualization/assets/map not joined.glb" > original.glb
    .venv/Scripts/python software/tools/weld_map.py original.glb "visualization/assets/map not joined.glb"
"""
import json
import struct
import sys

import numpy as np

COMPONENT = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}
SIZE = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def read_glb(path):
    with open(path, "rb") as f:
        magic, version, _ = struct.unpack("<4sII", f.read(12))
        assert magic == b"glTF" and version == 2
        json_len, json_type = struct.unpack("<I4s", f.read(8))
        assert json_type == b"JSON"
        gltf = json.loads(f.read(json_len))
        bin_len, bin_type = struct.unpack("<I4s", f.read(8))
        assert bin_type == b"BIN\x00"
        blob = f.read(bin_len)
    return gltf, blob


def accessor(gltf, blob, index):
    a = gltf["accessors"][index]
    view = gltf["bufferViews"][a["bufferView"]]
    assert "byteStride" not in view or view["byteStride"] == np.dtype(COMPONENT[a["componentType"]]).itemsize * SIZE[a["type"]]
    start = view.get("byteOffset", 0) + a.get("byteOffset", 0)
    count = a["count"] * SIZE[a["type"]]
    data = np.frombuffer(blob, COMPONENT[a["componentType"]], count, start)
    return data.reshape(a["count"], SIZE[a["type"]]) if SIZE[a["type"]] > 1 else data


def weld(positions, indices):
    """Unique positions (bit-identical) in order of first use, and the indices into them"""
    keys = np.ascontiguousarray(positions).view(np.uint32)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    order = np.argsort(first)
    remap = np.empty_like(order)
    remap[order] = np.arange(order.size)
    new_positions = positions[first[order]]
    new_indices = remap[inverse.reshape(-1)[indices]]
    return new_positions, new_indices


def main(source, target):
    gltf, blob = read_glb(source)
    out_views, out_accessors, chunks = [], [], []
    offset = 0
    meshes = []
    stats = {"vertices_before": 0, "vertices_after": 0, "triangles": 0}

    def add(data, target_kind, acc):
        nonlocal offset
        raw = data.tobytes()
        pad = (-offset) % 4
        if pad:
            chunks.append(b"\x00" * pad)
            offset += pad
        out_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(raw), "target": target_kind})
        chunks.append(raw)
        offset += len(raw)
        acc["bufferView"] = len(out_views) - 1
        out_accessors.append(acc)
        return len(out_accessors) - 1

    for mesh in gltf["meshes"]:
        primitives = []
        for prim in mesh["primitives"]:
            assert prim.get("mode", 4) == 4
            positions = accessor(gltf, blob, prim["attributes"]["POSITION"]).astype(np.float32)
            indices = accessor(gltf, blob, prim["indices"]).astype(np.int64)
            new_positions, new_indices = weld(positions, indices)
            # The same triangles, bit for bit
            assert np.array_equal(new_positions[new_indices].view(np.uint32), positions[indices].view(np.uint32))
            stats["vertices_before"] += len(positions)
            stats["vertices_after"] += len(new_positions)
            stats["triangles"] += len(indices) // 3
            small = len(new_positions) < 65535
            index_data = new_indices.astype(np.uint16 if small else np.uint32)
            pos_acc = add(new_positions, 34962, {"componentType": 5126, "count": len(new_positions), "type": "VEC3",
                "min": new_positions.min(axis=0).tolist(), "max": new_positions.max(axis=0).tolist()})
            idx_acc = add(index_data, 34963, {"componentType": 5123 if small else 5125, "count": len(index_data),
                "type": "SCALAR"})
            primitives.append({"attributes": {"POSITION": pos_acc}, "indices": idx_acc, "mode": 4, "material": 0})
        new_mesh = {k: v for k, v in mesh.items() if k != "primitives"}
        new_mesh["primitives"] = primitives
        meshes.append(new_mesh)

    out = {k: v for k, v in gltf.items() if k not in ("meshes", "accessors", "bufferViews", "buffers", "materials")}
    out["asset"] = dict(gltf.get("asset", {"version": "2.0"}))
    out["asset"]["generator"] = "weld_map.py (from %s)" % out["asset"].get("generator", "the source map")
    out["meshes"] = meshes
    out["accessors"] = out_accessors
    out["bufferViews"] = out_views
    out["materials"] = [{"name": "map", "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 1.0}}]
    binary = b"".join(chunks)
    binary += b"\x00" * ((-len(binary)) % 4)
    out["buffers"] = [{"byteLength": len(binary)}]
    text = json.dumps(out, separators=(",", ":")).encode("utf-8")
    text += b" " * ((-len(text)) % 4)
    with open(target, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(text) + 8 + len(binary)))
        f.write(struct.pack("<I4s", len(text), b"JSON"))
        f.write(text)
        f.write(struct.pack("<I4s", len(binary), b"BIN\x00"))
        f.write(binary)
    # The file as written keeps the nodes (names, hierarchy, transforms: the import settings are keyed by node name),
    # the scenes and the mesh names
    written, _ = read_glb(target)
    assert written["nodes"] == gltf["nodes"] and written["scenes"] == gltf["scenes"]
    assert [m.get("name") for m in written["meshes"]] == [m.get("name") for m in gltf["meshes"]]
    stats["bytes"] = 12 + 8 + len(text) + 8 + len(binary)
    print(json.dumps(stats))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
