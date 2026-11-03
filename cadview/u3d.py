"""Decode the mesh and scene subset of Universal 3D (ECMA-363) used by 3D PDFs.

Only base CLOD meshes are decoded. Progressive refinements, line/point sets,
textures and animation are ignored; exporters such as Altium write complete
base meshes, so the base resolution is the full model.
"""

import struct

import numpy as np

STATIC_FULL = 0x400
MAX_RANGE = 0x4000
ELEPHANT = 0x1FFF
MAX_HISTOGRAM_SYMBOL = 0xFFFF
SHADING_CONTEXT = 1

NO_COMPRESSION = 0x4
UNITS_DEFINED = 0x8

FILE_HEADER = 0x00443355
MODIFIER_CHAIN = 0xFFFFFF14
GROUP_NODE = 0xFFFFFF21
MODEL_NODE = 0xFFFFFF22
CLOD_DECLARATION = 0xFFFFFF31
CLOD_BASE_CONTINUATION = 0xFFFFFF3B
SHADING_MODIFIER = 0xFFFFFF45
LIT_TEXTURE_SHADER = 0xFFFFFF53
MATERIAL_RESOURCE = 0xFFFFFF54

_SWAP8 = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


class _Histogram:
    """Adaptive frequencies for one dynamic context; symbol 0 is the escape."""

    __slots__ = ("counts", "total")

    def __init__(self):
        self.counts, self.total = [1], 1

    def cumulative(self, symbol):
        return sum(self.counts[:symbol])

    def find(self, frequency):
        cumulative = 0
        for symbol, count in enumerate(self.counts):
            if frequency < cumulative + count:
                return symbol, cumulative, count
            cumulative += count
        raise ValueError("Corrupt U3D arithmetic-coded data")

    def add(self, symbol):
        if symbol >= MAX_HISTOGRAM_SYMBOL:
            return
        if symbol >= len(self.counts):
            self.counts.extend([0] * (symbol + 1 - len(self.counts)))
        self.counts[symbol] += 1
        self.total += 1
        if self.total >= ELEPHANT:
            self.counts = [count >> 1 for count in self.counts]
            self.counts[0] += 1
            self.total = sum(self.counts)


class BitReader:
    """ECMA-363 section 10 arithmetic decoder over one block's data."""

    def __init__(self, data: bytes, compressed=True):
        self.data, self.compressed = data, compressed
        self.bits = len(data) * 8
        self.position = 0
        self.low, self.high, self.underflow = 0, 0xFFFF, 0
        self.contexts = {}

    def _bit(self, position):
        if position >= self.bits:
            return 0  # The final symbol's code window may extend past the data.
        return (self.data[position >> 3] >> (position & 7)) & 1

    def _code(self):
        code = self._bit(self.position)
        start = self.position + 1 + self.underflow
        for offset in range(15):
            code = (code << 1) | self._bit(start + offset)
        return code

    def _raw(self):
        # With a reset coder, a static 256-symbol read consumes exactly eight bits.
        return self.low == 0 and self.high == 0xFFFF and self.underflow == 0 and not self.position & 7

    def symbol(self, context):
        if STATIC_FULL <= context < MAX_RANGE:
            total = context - STATIC_FULL
            histogram = None
        else:
            histogram = self.contexts.setdefault(context, _Histogram())
            total = histogram.total
        low, high = self.low, self.high
        span = high + 1 - low
        frequency = (total * (1 + self._code() - low) - 1) // span
        if histogram is None:
            symbol, cumulative, count = frequency + 1, frequency, 1
        else:
            symbol, cumulative, count = histogram.find(frequency)
        high = low - 1 + span * (cumulative + count) // total
        low = low + span * cumulative // total
        if histogram is not None:
            histogram.add(symbol)
        consumed = 0
        while (low & 0x8000) == (high & 0x8000):
            low = (low & 0x7FFF) << 1
            high = ((high & 0x7FFF) << 1) | 1
            consumed += 1
        if consumed:
            consumed += self.underflow
            self.underflow = 0
        while not high & 0x4000 and low & 0x4000:
            low = (low & 0x3FFF) << 1
            high = ((high & 0x3FFF) << 1) | 0x8001
            self.underflow += 1
        self.low, self.high = low, high
        self.position += consumed
        return symbol

    def u8(self):
        if self._raw() and self.position + 8 <= self.bits:
            value = self.data[self.position >> 3]
            self.position += 8
            return value
        return _SWAP8[self.symbol(STATIC_FULL + 256) - 1]

    def raw_bytes(self, count):
        if self._raw() and self.position + count * 8 <= self.bits:
            start = self.position >> 3
            self.position += count * 8
            return self.data[start:start + count]
        return bytes(self.u8() for _ in range(count))

    def u16(self):
        return struct.unpack("<H", self.raw_bytes(2))[0]

    def u32(self):
        return struct.unpack("<I", self.raw_bytes(4))[0]

    def f32(self, count=1):
        return np.frombuffer(self.raw_bytes(4 * count), "<f4").astype(np.float32)

    def string(self):
        return self.raw_bytes(self.u16()).decode("utf-8", "replace")

    def compressed_u32(self, context):
        if self.compressed and context and context < MAX_RANGE:
            symbol = self.symbol(context)
            if symbol:
                return symbol - 1
            value = self.u32()
            if context < STATIC_FULL:
                self.contexts[context].add(value + 1)
            return value
        return self.u32()


def blocks(data, offset=0, end=None):
    end = len(data) if end is None else end
    while offset + 12 <= end:
        kind, size, meta = struct.unpack_from("<III", data, offset)
        start = offset + 12
        if start + size > end:
            raise ValueError("Truncated U3D block")
        yield kind, data[start:start + size]
        offset = start + size + (-size % 4) + meta + (-meta % 4)


def _mesh_declaration(reader):
    name = reader.string()
    reader.u32()  # chain index
    attributes = reader.u32()
    for _ in range(6):
        reader.u32()  # maximum face/position/normal/color/texture counts
    shadings = []
    for _ in range(reader.u32()):
        shading_attributes = reader.u32()
        layers = [reader.u32() for _ in range(reader.u32())]
        reader.u32()  # original shading ID
        shadings.append((shading_attributes, len(layers)))
    return name, {"normals": not attributes & 1, "shadings": shadings}


def _base_mesh(reader, declarations):
    name = reader.string()
    reader.u32()  # chain index
    declaration = declarations.get(name)
    if declaration is None:
        return name, None
    faces, positions, normals, diffuse, specular, textures = (reader.u32() for _ in range(6))
    position = reader.f32(3 * positions).reshape(-1, 3)
    reader.f32(3 * normals)  # Creased normals are recomputed per face corner.
    diffuse_colors = reader.f32(4 * diffuse).reshape(-1, 4)
    reader.f32(4 * specular + 4 * textures)
    if not declaration["normals"]:
        normals = 0
    if not reader.compressed and len({shading for shading in declaration["shadings"]}) == 1:
        # Uncompressed indices are plain U32s; read every face at once.
        attributes, layers = declaration["shadings"][0]
        stride = 1 + 3 * (1 + bool(normals) + bool(attributes & 1) + bool(attributes & 2) + layers)
        table = np.frombuffer(reader.raw_bytes(4 * stride * faces), "<u4").reshape(faces, stride)
        if faces and table[:, 0].max() >= len(declaration["shadings"]):
            raise ValueError(f"U3D mesh {name!r} uses an undeclared shading")
        step = stride // 3
        corners = np.ascontiguousarray(table[:, 1::step][:, :3])
        if positions and corners.size and corners.max() >= positions:
            raise ValueError(f"U3D mesh {name!r} has an invalid position index")
        colors = None
        if diffuse and attributes & 1:
            colors = diffuse_colors[np.clip(table[:, 2 + bool(normals)::step][:, :3], 0, diffuse - 1)][..., :3]
        return name, {"position": position, "faces": corners, "shading": table[:, 0].copy(), "colors": colors}
    corners = np.empty((faces, 3), np.uint32)
    shading = np.empty(faces, np.uint32)
    corner_colors = np.zeros((faces, 3), np.int64) - 1
    for face in range(faces):
        shading_id = reader.compressed_u32(SHADING_CONTEXT)
        if shading_id >= len(declaration["shadings"]):
            raise ValueError(f"U3D mesh {name!r} uses an undeclared shading")
        shading[face] = shading_id
        attributes, layers = declaration["shadings"][shading_id]
        for corner in range(3):
            corners[face, corner] = reader.compressed_u32(STATIC_FULL + positions)
            if normals:
                reader.compressed_u32(STATIC_FULL + normals)
            if attributes & 1:
                corner_colors[face, corner] = reader.compressed_u32(STATIC_FULL + diffuse)
            if attributes & 2:
                reader.compressed_u32(STATIC_FULL + specular)
            for _ in range(layers):
                reader.compressed_u32(STATIC_FULL + textures)
    if positions and corners.size and corners.max() >= positions:
        raise ValueError(f"U3D mesh {name!r} has an invalid position index")
    colors = None
    if diffuse and (corner_colors >= 0).any():
        colors = np.where((corner_colors >= 0)[..., None], diffuse_colors[np.clip(corner_colors, 0, None)][..., :3], np.nan)
    return name, {"position": position, "faces": corners, "shading": shading, "colors": colors}


def _node(reader, model):
    name_parents = []
    for _ in range(reader.u32()):
        parent = reader.string()
        name_parents.append((parent, reader.f32(16).reshape(4, 4).T.astype(np.float64)))
    resource = reader.string() if model else None
    return {"parents": name_parents, "resource": resource}


def _modifier_chain(data, scene):
    reader = BitReader(data)
    chain = reader.string()
    reader.u32()  # chain type
    attributes = reader.u32()
    reader.f32((4 if attributes & 1 else 0) + (6 if attributes & 2 else 0))
    offset = (reader.position >> 3) + (-(reader.position >> 3) % 4)
    _read_blocks(data, offset + 4, scene, chain)  # after the modifier count


def _read_blocks(data, offset, scene, chain=None):
    compressed = scene["compressed"]
    for kind, body in blocks(data, offset):
        if kind == MODIFIER_CHAIN:
            _modifier_chain(body, scene)
            continue
        reader = BitReader(body, compressed)
        if kind in (GROUP_NODE, MODEL_NODE):
            name = reader.string()
            scene["nodes"][name] = _node(reader, kind == MODEL_NODE)
        elif kind == CLOD_DECLARATION:
            name, declaration = _mesh_declaration(reader)
            scene["declarations"][name] = declaration
        elif kind == CLOD_BASE_CONTINUATION:
            name, mesh = _base_mesh(reader, scene["declarations"])
            if mesh is not None:
                scene["meshes"][name] = mesh
        elif kind == SHADING_MODIFIER:
            reader.string()
            reader.u32()  # chain index
            reader.u32()  # shading attributes
            lists = [[reader.string() for _ in range(reader.u32())] for _ in range(reader.u32())]
            scene["shading"][chain] = lists
        elif kind == LIT_TEXTURE_SHADER:
            name = reader.string()
            reader.u32(); reader.f32(); reader.u32(); reader.u32(); reader.u32(); reader.u32(); reader.u32()
            scene["shaders"][name] = reader.string()
        elif kind == MATERIAL_RESOURCE:
            name = reader.string()
            reader.u32()  # material attributes
            ambient, diffuse, specular, emissive = (reader.f32(3) for _ in range(4))
            reader.f32()  # reflectivity
            scene["materials"][name] = (diffuse, float(reader.f32()[0]))


def parse(data: bytes) -> dict:
    """Return decoded meshes, scene nodes and materials from a U3D file."""
    if len(data) < 12 or struct.unpack_from("<I", data)[0] != FILE_HEADER:
        raise ValueError("Not a U3D file")
    header = BitReader(next(blocks(data))[1])
    header.u32()  # major and minor version
    profile = header.u32()
    header.u32(); header.raw_bytes(8); header.u32()  # declaration size, file size, encoding
    units = struct.unpack("<d", header.raw_bytes(8))[0] if profile & UNITS_DEFINED else None
    scene = {"nodes": {}, "declarations": {}, "meshes": {}, "shading": {}, "shaders": {}, "materials": {},
             "compressed": not profile & NO_COMPRESSION, "units": units}
    _read_blocks(data, 0, scene)
    return scene
