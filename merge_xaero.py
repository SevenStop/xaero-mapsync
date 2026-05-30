#!/usr/bin/env python3
"""
merge_xaero.py – merge two Xaero's World Map .zip region files.
Patch chunks overwrite base chunks, mirroring Region::mergeCopy() + XaeroMerger.

Usage:
    python merge_xaero.py --base <base.zip> --patch <patch.zip> --output <out.zip>
"""

import argparse
import copy
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from typing import Optional

# ── Version constants ─────────────────────────────────────────────────────────
# mirrors XAERO_REGION_VERSION_MAJOR / _MINOR from CMakeLists.txt
_MAJOR = 7
_MINOR = 8


# ── BitView ───────────────────────────────────────────────────────────────────
# Mirrors C++ BitView<T>: reads bits from an integer, LSB first.

class BitView:
    __slots__ = ('_v', '_pos')

    def __init__(self, value: int):
        self._v   = value
        self._pos = 0

    def get(self, n: int) -> int:
        v = (self._v >> self._pos) & ((1 << n) - 1)
        self._pos += n
        return v

    def peek(self, n: int) -> int:
        return (self._v >> self._pos) & ((1 << n) - 1)

    def skip(self, n: int):
        self._pos += n

    def skip_to_next_byte(self):
        # mirrors: position = ((position >> 3) + 1) << 3
        self._pos = ((self._pos >> 3) + 1) << 3


# ── BitWriter ─────────────────────────────────────────────────────────────────
# Mirrors C++ BitWriter<T>: places bits into an integer, LSB first.

class BitWriter:
    __slots__ = ('_v', '_pos')

    def __init__(self):
        self._v   = 0
        self._pos = 0

    def write(self, value: int, n: int):
        mask = (1 << n) - 1
        self._v |= (value & mask) << self._pos
        self._pos += n

    def skip(self, n: int):
        self._pos += n

    def skip_to_next_byte(self):
        self._pos = ((self._pos >> 3) + 1) << 3

    def as_u8(self) -> int:
        return self._v & 0xFF

    def as_u32(self) -> int:
        return self._v & 0xFFFFFFFF


# ── Stream ────────────────────────────────────────────────────────────────────
# Big-endian binary reader – mirrors ByteInputStream.

class Stream:
    __slots__ = ('_d', '_p')

    def __init__(self, data: bytes):
        self._d = data
        self._p = 0

    def eof(self) -> bool:
        return self._p >= len(self._d)

    def peek_byte(self) -> int:
        return self._d[self._p] if self._p < len(self._d) else -1

    def peek_i32(self) -> int:
        return struct.unpack_from('>i', self._d, self._p)[0]

    def u8(self) -> int:
        v = self._d[self._p]; self._p += 1; return v

    def i8(self) -> int:
        v = struct.unpack_from('>b', self._d, self._p)[0]; self._p += 1; return v

    def i16(self) -> int:
        v = struct.unpack_from('>h', self._d, self._p)[0]; self._p += 2; return v

    def u16(self) -> int:
        v = struct.unpack_from('>H', self._d, self._p)[0]; self._p += 2; return v

    def i32(self) -> int:
        v = struct.unpack_from('>i', self._d, self._p)[0]; self._p += 4; return v

    def u32(self) -> int:
        v = struct.unpack_from('>I', self._d, self._p)[0]; self._p += 4; return v

    def skip(self, n: int):
        self._p += n

    def read(self, n: int) -> bytes:
        v = self._d[self._p:self._p + n]; self._p += n; return v

    def bv32(self) -> BitView:
        return BitView(self.u32())

    def bv8(self) -> BitView:
        return BitView(self.u8())

    def mutf(self) -> str:
        """Java Modified UTF-8 string (uint16 length prefix)."""
        n = self.u16()
        return self.read(n).decode('utf-8', errors='replace') if n else ''


# ── NBT reader ────────────────────────────────────────────────────────────────
# Same minimal reader used by render_xaero.py.

def _nbt_str(s: Stream) -> str:
    return s.read(s.u16()).decode('utf-8', errors='replace')


def _nbt_skip(s: Stream, tag: int):
    if   tag == 1:  s.skip(1)
    elif tag == 2:  s.skip(2)
    elif tag in (3, 5): s.skip(4)
    elif tag in (4, 6): s.skip(8)
    elif tag == 7:  s.skip(s.i32())
    elif tag == 8:  s.skip(s.u16())
    elif tag == 9:
        et = s.u8(); count = s.i32()
        for _ in range(count): _nbt_skip(s, et)
    elif tag == 10:
        while True:
            t = s.u8()
            if t == 0: break
            _nbt_str(s)
            _nbt_skip(s, t)
    elif tag == 11: s.skip(s.i32() * 4)
    elif tag == 12: s.skip(s.i32() * 8)


def read_block_nbt(s: Stream) -> tuple:
    """Returns (name: str, props: dict[str, str])."""
    assert s.u8() == 10, "expected TAG_Compound"
    s.skip(s.u16())   # compound name (always empty)
    name = ''
    props = {}
    while True:
        t = s.u8()
        if t == 0:
            break
        key = _nbt_str(s)
        if t == 8:
            val = _nbt_str(s)
            if key == 'Name':
                name = val
        elif t == 10:   # Properties sub-compound
            while True:
                pt = s.u8()
                if pt == 0: break
                pk = _nbt_str(s)
                if pt == 8: props[pk] = _nbt_str(s)
                else: _nbt_skip(s, pt)
        else:
            _nbt_skip(s, t)
    return name, props


# ── NBT writer ────────────────────────────────────────────────────────────────
# Mirrors writeNBT() in RegionTools.cpp.

def _write_nbt_str(buf: bytearray, s: str):
    enc = s.encode('utf-8')
    buf += struct.pack('>H', len(enc))
    buf += enc


def write_block_nbt(buf: bytearray, name: str, props: dict):
    buf.append(10)                  # TAG_Compound
    buf += struct.pack('>H', 0)     # unnamed root
    buf.append(8)                   # TAG_String "Name"
    _write_nbt_str(buf, 'Name')
    _write_nbt_str(buf, name)
    if props:
        buf.append(10)              # TAG_Compound "Properties"
        _write_nbt_str(buf, 'Properties')
        for k, v in props.items():
            buf.append(8)
            _write_nbt_str(buf, k)
            _write_nbt_str(buf, v)
        buf.append(0)               # TAG_End (Properties)
    buf.append(0)                   # TAG_End (root)


# ── MUTF write helper ─────────────────────────────────────────────────────────
# Mirrors ByteOutputStream::writeMUTF. For ASCII biome names length == byte count.

def _write_mutf(buf: bytearray, s: str):
    enc = s.encode('utf-8')
    buf += struct.pack('>H', len(enc))
    buf += enc


# ── Name helpers ──────────────────────────────────────────────────────────────

def _strip(name: str) -> str:
    """mirrors xaero::stripName – removes 'minecraft:' prefix."""
    return name[10:] if len(name) > 10 and name[9] == ':' else name


# ── Legacy tables ─────────────────────────────────────────────────────────────
# mirrors getBiomeFromID() and fixBiome() in LegacyCompatibility.cpp

_BIOME_ID_TABLE = {
    0:"ocean",1:"plains",2:"desert",3:"mountains",4:"forest",5:"taiga",
    6:"swamp",7:"river",8:"nether_wastes",9:"the_end",10:"frozen_ocean",
    11:"frozen_river",12:"snowy_tundra",13:"snowy_mountains",14:"mushroom_fields",
    15:"mushroom_field_shore",16:"beach",17:"desert_hills",18:"wooded_hills",
    19:"taiga_hills",20:"mountain_edge",21:"jungle",22:"jungle_hills",
    23:"jungle_edge",24:"deep_ocean",25:"stone_shore",26:"snowy_beach",
    27:"birch_forest",28:"birch_forest_hills",29:"dark_forest",30:"snowy_taiga",
    31:"snowy_taiga_hills",32:"giant_tree_taiga",33:"giant_tree_taiga_hills",
    34:"wooded_mountains",35:"savanna",36:"savanna_plateau",37:"badlands",
    38:"wooded_badlands_plateau",39:"badlands_plateau",40:"small_end_islands",
    41:"end_midlands",42:"end_highlands",43:"end_barrens",44:"warm_ocean",
    45:"lukewarm_ocean",46:"cold_ocean",47:"deep_warm_ocean",48:"deep_lukewarm_ocean",
    49:"deep_cold_ocean",50:"deep_frozen_ocean",
    127:"the_void",129:"sunflower_plains",130:"desert_lakes",
    131:"gravelly_mountains",132:"flower_forest",133:"taiga_mountains",
    134:"swamp_hills",140:"ice_spikes",149:"modified_jungle",
    151:"modified_jungle_edge",155:"tall_birch_forest",156:"tall_birch_hills",
    157:"dark_forest_hills",158:"snowy_taiga_mountains",160:"giant_spruce_taiga",
    161:"giant_spruce_taiga_hills",162:"modified_gravelly_mountains",
    163:"shattered_savanna",164:"shattered_savanna_plateau",165:"eroded_badlands",
    166:"modified_wooded_badlands_plateau",167:"modified_badlands_plateau",
    168:"bamboo_jungle",169:"bamboo_jungle_hills",170:"soul_sand_valley",
    171:"crimson_forest",172:"warped_forest",173:"basalt_deltas",
    174:"dripstone_caves",175:"lush_caves",177:"meadow",178:"grove",
    179:"snowy_slopes",180:"snowcapped_peaks",181:"lofty_peaks",182:"stony_peaks",
}

_BIOME_FIX = {
    "badlands_plateau":"badlands","bamboo_jungle_hills":"bamboo_jungle",
    "birch_forest_hills":"birch_forest","dark_forest_hills":"dark_forest",
    "desert_hills":"desert","desert_lakes":"desert",
    "giant_spruce_taiga_hills":"old_growth_spruce_taiga",
    "giant_spruce_taiga":"old_growth_spruce_taiga",
    "giant_tree_taiga_hills":"old_growth_pine_taiga",
    "giant_tree_taiga":"old_growth_pine_taiga",
    "gravelly_mountains":"windswept_gravelly_hills","jungle_edge":"sparse_jungle",
    "jungle_hills":"jungle","modified_badlands_plateau":"badlands",
    "modified_gravelly_mountains":"windswept_gravelly_hills",
    "modified_jungle_edge":"sparse_jungle","modified_jungle":"jungle",
    "modified_wooded_badlands_plateau":"wooded_badlands",
    "mountain_edge":"windswept_hills","mountains":"windswept_hills",
    "mushroom_field_shore":"mushroom_fields","shattered_savanna":"windswept_savanna",
    "shattered_savanna_plateau":"windswept_savanna","snowy_mountains":"snowy_plains",
    "snowy_taiga_hills":"snowy_taiga","snowy_taiga_mountains":"snowy_taiga",
    "snowy_tundra":"snowy_plains","stone_shore":"stony_shore",
    "swamp_hills":"swamp","taiga_hills":"taiga","taiga_mountains":"taiga",
    "tall_birch_forest":"old_growth_birch_forest",
    "tall_birch_hills":"old_growth_birch_forest",
    "wooded_badlands_plateau":"wooded_badlands","wooded_hills":"forest",
    "wooded_mountains":"windswept_forest","lofty_peaks":"jagged_peaks",
    "snowcapped_peaks":"frozen_peaks",
}


def _biome_from_id(bid: int) -> str:
    return _BIOME_ID_TABLE.get(bid, '') or 'plains'


def _convert_nbt(name: str, props: dict, major: int) -> tuple:
    """mirrors convertNBT() in LegacyCompatibility.cpp"""
    s = _strip(name)
    if major == 1:
        name = {"stone_slab":"minecraft:smooth_stone_slab",
                "sign":"minecraft:oak_sign",
                "wall_sign":"minecraft:oak_wall_sign"}.get(s, name)
        s = _strip(name)
    if major < 3:
        if s == "jigsaw":
            orient = {"":"north_up","down":"down_south","up":"up_north",
                      "north":"north_up","south":"south_up",
                      "west":"west_up","east":"east_up"}
            props = {"orientation": orient.get(props.get("facing", ""), "north_up")}
        elif s == "redstone_wire":
            n, so, e, w = (props.get(d, "") for d in ("north", "south", "east", "west"))
            def _wf(v, a, b): return v if v else ("side" if not a and not b else "none")
            props = {"north": _wf(n, w, e), "south": _wf(so, w, e),
                     "east":  _wf(e, n, so), "west":  _wf(w, n, so)}
        elif s.endswith("_wall"):
            props = {k: ("low" if props.get(k) == "true" else "none")
                     for k in ("north", "south", "east", "west") if k in props}
    if major < 5:
        if s == "cauldron":
            if "level" in props: name = "minecraft:water_cauldron"
            else: props = {}
        elif s == "grass_path":
            name = "minecraft:dirt_path"
    if major < 7:
        if s == "creaking_heart" and "active" in props:
            props["creaking_heart_state"] = "awake" if props.pop("active") == "true" else "uprooted"
    return name, props


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Overlay:
    name: str
    props: dict
    light: int
    opacity: Optional[int]  # None = not set; mirrors std::optional<std::int32_t>


@dataclass
class Pixel:
    name: str
    props: dict
    height: int                    # 12-bit signed; mirrors std::int16_t
    light: int                     # 4-bit; mirrors std::uint8_t
    biome: Optional[str]           # stripped (no minecraft:); mirrors optional biome
    top_height: Optional[int]      # uint8; mirrors std::optional<std::uint8_t>
    overlays: list = field(default_factory=list)


@dataclass
class Chunk:
    pixels: Optional[list] = None  # 16x16 list[list[Pixel]]; None = not populated
    chunk_interp_ver: int = 0      # mirrors chunkInterpretationVersion (int8)
    cave_start: int = 0            # mirrors caveStart (int32)
    cave_depth: int = 0            # mirrors caveDepth (int8)


@dataclass
class TileChunk:
    chunks: Optional[list] = None  # 4x4 list[list[Chunk]]; None = not populated


@dataclass
class Region:
    tile_chunks: list = field(
        default_factory=lambda: [[TileChunk() for _ in range(8)] for _ in range(8)]
    )
    major: int = _MAJOR
    minor: int = _MINOR


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_region(data: bytes) -> Region:
    """
    Mirrors parseRegion(std::istream&, nullptr) from RegionTools.cpp.
    Parses without lookups, preserving all data for round-trip serialization.
    """
    s      = Stream(data)
    region = Region()
    major  = 0
    minor  = 0
    is115  = False

    if s.peek_byte() == 255:
        s.skip(1)
        major = s.i16()
        minor = s.i16()
        if major == 2 and minor >= 5:
            is115 = (s.u8() == 1)

    region.major = major
    region.minor = minor

    use_ctypes = (minor < 5) or (major <= 2 and not is115)

    state_pal = []   # list of (name, props)
    biome_pal = []   # list of str

    for _ in range(64):   # at most 64 tile-chunks per region
        if s.eof():
            break

        coords = s.bv8()
        tile_z = coords.get(4)
        tile_x = coords.get(4)

        tile         = region.tile_chunks[tile_x][tile_z]
        tile.chunks  = [[Chunk() for _ in range(4)] for _ in range(4)]

        for cx in range(4):
            for cz in range(4):
                chunk = tile.chunks[cx][cz]

                if s.peek_i32() == -1:
                    s.skip(4)
                    continue   # chunk not populated

                chunk.pixels = [[None] * 16 for _ in range(16)]

                for px in range(16):
                    for pz in range(16):
                        par = s.bv32()

                        not_grass = par.get(1)
                        has_ov    = par.get(1)
                        ctype     = par.peek(2) if use_ctypes else 0
                        par.skip(2)

                        if minor == 2:
                            has_slope = par.get(1)
                        else:
                            par.skip(1)
                            has_slope = False

                        par.skip(1)                  # ignored bit
                        h_in_par  = not par.get(1)   # stored inverted
                        par.skip_to_next_byte()

                        light = par.get(4)
                        if h_in_par:
                            h_low = par.get(8)
                        else:
                            par.skip(8)
                            h_low = 0

                        has_biome = par.get(1)
                        new_state = par.get(1)
                        new_biome = par.get(1)
                        biome_int = par.get(1)
                        top_diff  = par.get(1) if minor >= 4 else False

                        if h_in_par:
                            h_high = par.get(4)
                            height = (h_low | (h_high << 8)) & 0x0FFF
                            if height & 0x0800:   # sign-extend 12-bit
                                height -= 0x1000
                        else:
                            height = 0

                        # ── block state ───────────────────────────────────────
                        if not_grass:
                            if major == 0:
                                s.i32()   # legacy numeric state id (no lookup table)
                                name, props = 'minecraft:air', {}
                            elif new_state:
                                name, props = read_block_nbt(s)
                                if major < 7:
                                    name, props = _convert_nbt(name, props, major)
                                state_pal.append((name, props))
                            else:
                                name, props = state_pal[s.i32()]
                        else:
                            name, props = 'minecraft:grass_block', {}

                        if not h_in_par:
                            height = s.u8()

                        top_height = None
                        if top_diff:
                            top_height = s.u8()

                        # ── overlays ──────────────────────────────────────────
                        overlays = []
                        if has_ov:
                            for _ in range(s.u8()):
                                op = s.bv32()
                                is_water = not op.get(1)
                                leg_opac = op.get(1)
                                cust_col = op.get(1)
                                has_opac = op.get(1)
                                ov_light = op.get(4)
                                if use_ctypes:
                                    ov_ctype = op.get(2)
                                else:
                                    op.skip(2)
                                    ov_ctype = 0
                                new_ov   = op.get(1)
                                ov_opacity = op.get(4) if minor >= 8 else None

                                if is_water:
                                    on, op2 = 'minecraft:water', {}
                                elif major == 0:
                                    s.i32()
                                    on, op2 = 'minecraft:air', {}
                                elif new_ov:
                                    on, op2 = read_block_nbt(s)
                                    if major < 7:
                                        on, op2 = _convert_nbt(on, op2, major)
                                    state_pal.append((on, op2))
                                else:
                                    on, op2 = state_pal[s.u32()]

                                if minor < 1 and leg_opac:
                                    s.skip(4)
                                if ov_ctype == 2 or cust_col:
                                    s.skip(4)
                                if minor < 8 and has_opac:
                                    ov_opacity = s.i32()

                                overlays.append(Overlay(on, op2, ov_light, ov_opacity))

                        # ── biome ─────────────────────────────────────────────
                        biome_name = None
                        if use_ctypes and ctype == 3:   # CUSTOM_BIOME
                            s.skip(4)
                        if (use_ctypes and ctype in (1, 2)) or has_biome:
                            if major < 4:
                                bb  = s.u8()
                                bid = s.i32() if minor >= 3 and bb >= 255 else bb
                                biome_name = _biome_from_id(bid)
                                if major < 6:
                                    biome_name = _BIOME_FIX.get(biome_name, biome_name)
                            else:
                                if new_biome:
                                    if biome_int:
                                        bid = s.i32()
                                        biome_name = _biome_from_id(bid)
                                        if major < 6:
                                            biome_name = _BIOME_FIX.get(biome_name, biome_name)
                                    else:
                                        raw = s.mutf()
                                        biome_name = _strip(raw)
                                        if major < 6:
                                            biome_name = _BIOME_FIX.get(biome_name, biome_name)
                                    biome_pal.append(biome_name)
                                else:
                                    biome_name = biome_pal[s.u32()]

                        if minor == 2 and has_slope:
                            s.skip(1)

                        chunk.pixels[px][pz] = Pixel(
                            name=name, props=props,
                            height=height, light=light,
                            biome=biome_name, top_height=top_height,
                            overlays=overlays,
                        )

                # chunk metadata (written at end of chunk's 16x16 pixels)
                if minor >= 4:
                    chunk.chunk_interp_ver = s.i8()
                if minor >= 6:
                    chunk.cave_start = s.i32()
                    if minor >= 7:
                        chunk.cave_depth = s.i8()

    return region


# ── Merge ─────────────────────────────────────────────────────────────────────

def merge_copy(base: Region, patch: Region):
    """
    Mirrors Region::mergeCopy(const Region&).
    For every populated chunk in patch, copies it into base (overwriting).
    Both regions remain valid after the merge.
    """
    for tx in range(8):
        for tz in range(8):
            patch_tile = patch.tile_chunks[tx][tz]
            if patch_tile.chunks is None:
                continue
            for cx in range(4):
                for cz in range(4):
                    patch_chunk = patch_tile.chunks[cx][cz]
                    if patch_chunk.pixels is None:
                        continue
                    # allocate base tile on demand (mirrors tileChunk.allocateChunks())
                    base_tile = base.tile_chunks[tx][tz]
                    if base_tile.chunks is None:
                        base_tile.chunks = [[Chunk() for _ in range(4)] for _ in range(4)]
                    # deep-copy so both regions remain independent
                    base_tile.chunks[cx][cz] = copy.deepcopy(patch_chunk)


# ── Serializer ────────────────────────────────────────────────────────────────

def _state_eq(a: tuple, b: tuple) -> bool:
    """Mirrors BlockState::operator==: compare name and properties."""
    return a[0] == b[0] and a[1] == b[1]


def serialize_region(region: Region) -> bytes:
    """
    Mirrors serializeRegionImpl() from RegionTools.cpp.
    Always outputs at version 7.8 regardless of input version.
    """
    buf = bytearray()

    # header: 255 + major(uint16) + minor(uint16)
    buf.append(255)
    buf += struct.pack('>HH', _MAJOR, _MINOR)
    # Note: is115not114 byte is NOT written here (only emitted when major==2, minor>=5)

    state_pal = []   # list of (name, props) already written
    biome_pal = []   # list of stripped biome names already written

    for tile_x in range(8):
        for tile_z in range(8):
            tile = region.tile_chunks[tile_x][tile_z]
            if tile.chunks is None:
                continue

            # tile coordinate byte: bits [0:4] = tileZ, bits [4:8] = tileX  (LSB-first)
            bw = BitWriter()
            bw.write(tile_z, 4)
            bw.write(tile_x, 4)
            buf.append(bw.as_u8())

            for cx in range(4):
                for cz in range(4):
                    chunk = tile.chunks[cx][cz]
                    if chunk.pixels is None:
                        buf += struct.pack('>i', -1)
                        continue

                    for px in range(16):
                        for pz in range(16):
                            pixel    = chunk.pixels[px][pz]
                            name     = pixel.name
                            props    = pixel.props
                            is_grass = _strip(name) == 'grass_block'

                            # ── state palette lookup ───────────────────────────
                            state_in_pal  = is_grass
                            state_pal_idx = 0
                            if not is_grass:
                                for i, entry in enumerate(state_pal):
                                    if _state_eq(entry, (name, props)):
                                        state_in_pal  = True
                                        state_pal_idx = i
                                        break

                            # ── biome palette lookup ───────────────────────────
                            biome        = pixel.biome
                            biome_in_pal = (biome is None)
                            biome_pal_idx = 0
                            if biome is not None:
                                for i, b in enumerate(biome_pal):
                                    if b == biome:
                                        biome_in_pal  = True
                                        biome_pal_idx = i
                                        break

                            # ── parameters BitWriter<uint32> ──────────────────
                            # Layout (LSB-first):
                            #  0     not_grass
                            #  1     has_overlays
                            #  2-3   ColorType::NONE (0)
                            #  4-5   0  (slope/unused – skip)
                            #  6     0  (heightInParameters stored as !true)
                            #  7     0  (implicit after skipToNextByte)
                            #  8-11  light
                            # 12-19  height & 0xFF   (low byte)
                            # 20     has_biome
                            # 21     new_state_palette_entry
                            # 22     new_biome_palette_entry
                            # 23     0  (biome_as_int = false)
                            # 24     top_height_differs
                            # 25-28  (height >> 8) & 0xF  (high nibble)
                            pw = BitWriter()
                            pw.write(0 if is_grass else 1, 1)
                            pw.write(1 if pixel.overlays else 0, 1)
                            pw.write(0, 2)   # ColorType::NONE
                            pw.skip(2)       # slope / unused
                            pw.write(0, 1)   # heightInParameters = true (stored inverted)
                            pw.skip_to_next_byte()
                            pw.write(pixel.light & 0xF, 4)
                            pw.write(pixel.height & 0xFF, 8)
                            pw.write(1 if biome is not None else 0, 1)
                            pw.write(0 if state_in_pal else 1, 1)
                            pw.write(0 if biome_in_pal else 1, 1)
                            pw.write(0, 1)   # biome_as_int = false
                            pw.write(1 if pixel.top_height is not None else 0, 1)
                            pw.write((pixel.height >> 8) & 0xF, 4)
                            buf += struct.pack('>I', pw.as_u32())

                            # ── state ──────────────────────────────────────────
                            if not is_grass:
                                if state_in_pal:
                                    buf += struct.pack('>I', state_pal_idx)
                                else:
                                    write_block_nbt(buf, name, props)
                                    state_pal.append((name, props))

                            # ── top_height ─────────────────────────────────────
                            if pixel.top_height is not None:
                                buf.append(pixel.top_height & 0xFF)

                            # ── overlays ───────────────────────────────────────
                            if pixel.overlays:
                                buf.append(len(pixel.overlays) & 0xFF)
                                for ov in pixel.overlays:
                                    on, op2  = ov.name, ov.props
                                    is_water = _strip(on) == 'water'
                                    has_opa  = ov.opacity is not None

                                    ov_in_pal  = is_water
                                    ov_pal_idx = 0
                                    if not is_water:
                                        for i, entry in enumerate(state_pal):
                                            if _state_eq(entry, (on, op2)):
                                                ov_in_pal  = True
                                                ov_pal_idx = i
                                                break

                                    # overlay parameters BitWriter<uint32>
                                    # 0   not_water
                                    # 1   legacy_opacity = false
                                    # 2   custom_color = false
                                    # 3   has_opacity
                                    # 4-7  light
                                    # 8-9  ColorType::NONE
                                    # 10  new_overlay_state
                                    # 11-14 opacity (if has_opacity)
                                    opw = BitWriter()
                                    opw.write(0 if is_water else 1, 1)
                                    opw.write(0, 1)   # legacy_opacity
                                    opw.write(0, 1)   # custom_color
                                    opw.write(1 if has_opa else 0, 1)
                                    opw.write(ov.light & 0xF, 4)
                                    opw.write(0, 2)   # ColorType::NONE
                                    opw.write(0 if ov_in_pal else 1, 1)
                                    if has_opa:
                                        opw.write(ov.opacity & 0xF, 4)
                                    buf += struct.pack('>I', opw.as_u32())

                                    if not is_water:
                                        if ov_in_pal:
                                            buf += struct.pack('>I', ov_pal_idx)
                                        else:
                                            write_block_nbt(buf, on, op2)
                                            state_pal.append((on, op2))

                            # ── biome ──────────────────────────────────────────
                            if biome is not None:
                                if biome_in_pal:
                                    buf += struct.pack('>I', biome_pal_idx)
                                else:
                                    _write_mutf(buf, f'minecraft:{biome}')
                                    biome_pal.append(biome)

                    # chunk metadata – always written at v7.8 (mirrors minor>=4/6/7 guards)
                    buf.append(chunk.chunk_interp_ver & 0xFF)
                    buf += struct.pack('>i', chunk.cave_start)
                    buf.append(chunk.cave_depth & 0xFF)

    return bytes(buf)


# ── Write region ──────────────────────────────────────────────────────────────

def write_region(region: Region, path: str):
    """
    Mirrors writeRegion() + packRegionImpl().
    Creates a .zip containing region.xaero with DEFLATE compression.
    """
    data = serialize_region(region)
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('region.xaero', data)


# ── Utilities ─────────────────────────────────────────────────────────────────

def count_chunks(region: Region) -> int:
    """Mirrors countChunks() in XaeroMerger.cpp."""
    count = 0
    for tx in range(8):
        for tz in range(8):
            tile = region.tile_chunks[tx][tz]
            if tile.chunks is None:
                continue
            for cx in range(4):
                for cz in range(4):
                    if tile.chunks[cx][cz].pixels is not None:
                        count += 1
    return count


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Merge two Xaero map region files. Patch chunks overwrite base chunks.'
    )
    parser.add_argument('--base',   required=True, help='Base region file (.zip)')
    parser.add_argument('--patch',  required=True, help='Patch region file (.zip)')
    parser.add_argument('--output', required=True, help='Output file (.zip)')
    args = parser.parse_args()

    print(f'Parsing base: {args.base}')
    with zipfile.ZipFile(args.base) as zf:
        base_data = zf.read('region.xaero')
    base = parse_region(base_data)
    base_chunks = count_chunks(base)
    print(f'  Populated chunks: {base_chunks}/512')

    print(f'Parsing patch: {args.patch}')
    with zipfile.ZipFile(args.patch) as zf:
        patch_data = zf.read('region.xaero')
    patch = parse_region(patch_data)
    patch_chunks = count_chunks(patch)
    print(f'  Populated chunks: {patch_chunks}/512')

    print('Merging (patch overwrites base where both have data)...')
    merge_copy(base, patch)
    merged_chunks = count_chunks(base)
    print(f'  Result chunks:    {merged_chunks}/512')
    print(f'  New from patch:   {merged_chunks - base_chunks}')

    print(f'Writing: {args.output}')
    write_region(base, args.output)
    print('Done.')


if __name__ == '__main__':
    main()
