#!/usr/bin/env python3
"""
render_xaero.py - renders a Xaero's World Map .zip region file to PNG.
Mirrors the C++ parseRegion() + generateImage() pipeline exactly.

Usage:
    python render_xaero.py <region.zip> <BlockLookups.cpp> <output.png>
"""

import re
import struct
import sys
import zipfile

from PIL import Image


# ─── BitView ───────────────────────────────────────────────────────────────────
# Mirrors C++ BitView<T>: reads bits from an integer, LSB first.

class BitView:
    __slots__ = ('_v', '_pos')

    def __init__(self, value: int):
        self._v = value
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


# ─── Stream ────────────────────────────────────────────────────────────────────
# Big-endian binary reader, mirrors ByteInputStream (all integers are big-endian
# because xaero stores data in Java byte order).

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
        """Java Modified UTF-8 string (uint16 length prefix). ASCII-safe."""
        n = self.u16()
        return self.read(n).decode('utf-8', errors='replace') if n else ''


# ─── Minimal NBT reader ────────────────────────────────────────────────────────
# Only needs to handle the compound written by writeNBT() in RegionTools.cpp:
#   TAG_Compound (unnamed)
#     TAG_String "Name" = "minecraft:block_name"
#     TAG_Compound "Properties" (optional)
#       TAG_String "key" = "value"  ...
#       TAG_End
#   TAG_End

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
    """Returns (name: str, props: dict[str,str])."""
    assert s.u8() == 10, "expected TAG_Compound"
    s.skip(s.u16())  # compound name (always empty)
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
        elif t == 10:  # Properties sub-compound
            while True:
                pt = s.u8()
                if pt == 0: break
                pk = _nbt_str(s)
                if pt == 8: props[pk] = _nbt_str(s)
                else: _nbt_skip(s, pt)
        else:
            _nbt_skip(s, t)
    return name, props


# ─── Name helpers ─────────────────────────────────────────────────────────────

def _strip(name: str) -> str:
    """mirrors xaero::stripName — removes 'minecraft:' prefix."""
    return name[10:] if len(name) > 10 and name[9] == ':' else name


# ─── Lookup table parsing ─────────────────────────────────────────────────────
# Parses the generated BlockLookups.cpp to extract:
#   block_colors: { stripped_name: [(props_dict, (R,G,B,A), tint_index), ...] }
#   biome_colors: { biome_name: {'grass','water','foliage','dry_foliage'} }

# Regex for the nbt::tag_compound{...} properties argument.
# Handles: {}, {{"k","v"}}, {{"k1","v1"},{"k2","v2"}}, ...
_COMPOUND_RE = r'\{((?:\{"[^"]+",\s*"[^"]+"\}(?:,\s*\{"[^"]+",\s*"[^"]+"\})*)?)\}'
_PROP_PAIR   = re.compile(r'\{"([^"]+)",\s*"([^"]+)"\}')
_BS_PAT      = re.compile(
    r'BlockState\{"([^"]+)",\s*nbt::tag_compound' + _COMPOUND_RE +
    r',\s*ColorInfo\{\{(\d+),(\d+),(\d+),(\d+)\},([-\d]+)\}\}'
)
_BIOME_PAT   = re.compile(
    r'\{"([^"]+)",xaero::BiomeColors\{'
    r'\{(\d+),(\d+),(\d+),(\d+)\},'
    r'\{(\d+),(\d+),(\d+),(\d+)\},'
    r'\{(\d+),(\d+),(\d+),(\d+)\},'
    r'\{(\d+),(\d+),(\d+),(\d+)\}\}\}'
)


def load_lookups(cpp_path: str) -> tuple:
    with open(cpp_path) as f:
        src = f.read()

    # block colors
    s0 = src.find('const xaero::StateLookup')
    e0 = src.find('\n};', s0) + 3
    block_colors = {}
    for m in _BS_PAT.finditer(src[s0:e0]):
        name  = _strip(m.group(1))
        props = dict(_PROP_PAIR.findall(m.group(2)))
        rgba  = (int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6)))
        tint  = int(m.group(7))
        block_colors.setdefault(name, []).append((props, rgba, tint))

    # biome colors
    s1 = src.find('const xaero::BiomeLookup')
    e1 = src.find('\n};', s1) + 3
    biome_colors = {}
    for m in _BIOME_PAT.finditer(src[s1:e1]):
        n = [int(m.group(i)) for i in range(2, 18)]
        biome_colors[m.group(1)] = {
            'grass':      (n[0],  n[1],  n[2],  n[3]),
            'water':      (n[4],  n[5],  n[6],  n[7]),
            'foliage':    (n[8],  n[9],  n[10], n[11]),
            'dry_foliage':(n[12], n[13], n[14], n[15]),
        }

    return block_colors, biome_colors


# ─── Legacy biome tables ───────────────────────────────────────────────────────
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


# ─── Legacy NBT conversion ────────────────────────────────────────────────────
# mirrors convertNBT() in LegacyCompatibility.cpp

def _convert_nbt(name: str, props: dict, major: int) -> tuple:
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
            props = {"orientation": orient.get(props.get("facing",""), "north_up")}
        elif s == "redstone_wire":
            n,so,e,w = (props.get(d,"") for d in ("north","south","east","west"))
            def _wf(v,a,b): return v if v else ("side" if not a and not b else "none")
            props = {"north":_wf(n,w,e),"south":_wf(so,w,e),
                     "east":_wf(e,n,so),"west":_wf(w,n,so)}
        elif s.endswith("_wall"):
            props = {k:("low" if props.get(k)=="true" else "none")
                     for k in ("north","south","east","west") if k in props}
    if major < 5:
        if s == "cauldron":
            if "level" in props: name = "minecraft:water_cauldron"
            else: props = {}
        elif s == "grass_path":
            name = "minecraft:dirt_path"
    if major < 7:
        if s == "creaking_heart" and "active" in props:
            props["creaking_heart_state"] = "awake" if props.pop("active")=="true" else "uprooted"
    return name, props


# ─── Color rendering ──────────────────────────────────────────────────────────
# mirrors getStateColor() in RegionTools.cpp

_T_GRASS, _T_FOLIAGE, _T_DRY, _T_REDSTONE, _T_WATER = 0, 1, 2, 3, 4

def _get_color(name: str, props: dict, biome: dict, block_colors: dict) -> tuple:
    """Returns (R, G, B, A)."""
    s = _strip(name)
    entries = block_colors.get(s)
    if not entries:
        return (0, 0, 0, 0)

    # find best property match, fall back to first entry
    color, tint = entries[0][1], entries[0][2]
    for ep, ec, et in entries:
        if ep == props:
            color, tint = ec, et
            break

    # name-based tint overrides (mirrors the C++ overrides)
    if 'redstone' in s:            tint = _T_REDSTONE
    elif s == 'leaf_litter':       tint = _T_DRY
    elif 'leaves' in s or s=='vine': tint = _T_FOLIAGE
    elif 'water' in s:             tint = _T_WATER

    if tint < 0:
        return color

    r, g, b, a = color
    if   tint == _T_GRASS:    tr,tg,tb,_ = biome.get('grass',      (255,255,255,255))
    elif tint == _T_FOLIAGE:  tr,tg,tb,_ = biome.get('foliage',    (255,255,255,255))
    elif tint == _T_DRY:      tr,tg,tb,_ = biome.get('dry_foliage',(255,255,255,255))
    elif tint == _T_REDSTONE: tr,tg,tb   = 231, 6, 0
    elif tint == _T_WATER:    tr,tg,tb,_ = biome.get('water',      (255,255,255,255))
    else:                     return color

    return (r*tr//255, g*tg//255, b*tb//255, a)


# ─── Parser + renderer ────────────────────────────────────────────────────────
# mirrors parseRegion() + generateImage() from RegionTools.cpp

def parse_and_render(data: bytes, block_colors: dict, biome_colors: dict) -> Image.Image:
    s = Stream(data)
    img = Image.new('RGBA', (512, 512), (0, 0, 0, 0))
    pix = img.load()

    major, minor, is115 = 0, 0, False
    if s.peek_byte() == 255:
        s.skip(1)
        major = s.i16()
        minor = s.i16()
        if major == 2 and minor >= 5:
            is115 = (s.u8() == 1)

    use_ctypes = (minor < 5) or (major <= 2 and not is115)

    state_pal = []   # list of (name, props)
    biome_pal = []   # list of str

    for _ in range(64):   # max 64 tile chunks per region
        if s.eof():
            break

        coords = s.bv8()
        tile_z = coords.get(4)
        tile_x = coords.get(4)

        for cx in range(4):
            for cz in range(4):
                if s.peek_i32() == -1:
                    s.skip(4)
                    continue

                for px in range(16):
                    for pz in range(16):
                        par = s.bv32()

                        not_grass = par.get(1)           # bit 0
                        has_ov    = par.get(1)           # bit 1
                        ctype     = par.peek(2) if use_ctypes else 0
                        par.skip(2)                      # bits 2-3: color type
                        if minor == 2:
                            has_slope = par.get(1)       # bit 4
                        else:
                            par.skip(1)                  # bit 4: unused
                            has_slope = False
                        par.skip(1)                      # bit 5: ignored
                        h_in_par = not par.get(1)        # bit 6 (note: stored inverted)
                        par.skip_to_next_byte()          # skip bit 7, land on bit 8

                        light = par.get(4)               # bits 8-11
                        if h_in_par:
                            h_low = par.get(8)           # bits 12-19
                        else:
                            par.skip(8)
                            h_low = 0

                        has_biome  = par.get(1)          # bit 20
                        new_state  = par.get(1)          # bit 21
                        new_biome  = par.get(1)          # bit 22
                        biome_int  = par.get(1)          # bit 23
                        if minor >= 4:
                            top_diff = par.get(1)        # bit 24
                        else:
                            top_diff = False
                        if h_in_par:
                            h_high = par.get(4)          # bits 24/25 - 27/28
                            height = (h_low | (h_high << 8)) & 0x0FFF
                            if height & 0x0800:          # sign-extend 12-bit
                                height -= 0x1000

                        # ── block state ──────────────────────────────────────
                        if not_grass:
                            if major == 0:
                                s.i32()                  # legacy numeric state id
                                name, props = 'air', {}
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
                            s.u8()          # height byte (separate from params)
                        if top_diff:
                            s.skip(1)       # top_height byte

                        # ── overlays ─────────────────────────────────────────
                        overlays = []
                        if has_ov:
                            for _ in range(s.u8()):
                                op = s.bv32()
                                is_water  = not op.get(1)   # bit 0
                                leg_opac  = op.get(1)       # bit 1
                                cust_col  = op.get(1)       # bit 2
                                has_opac  = op.get(1)       # bit 3
                                ov_light  = op.get(4)       # bits 4-7
                                if use_ctypes:
                                    ov_ctype = op.get(2)    # bits 8-9
                                else:
                                    op.skip(2)
                                    ov_ctype = 0
                                new_ov    = op.get(1)       # bit 10
                                ov_alpha  = op.get(4) if minor >= 8 else 255  # bits 11-14

                                if is_water:
                                    on, op2 = 'minecraft:water', {}
                                elif major == 0:
                                    s.i32()
                                    on, op2 = 'air', {}
                                elif new_ov:
                                    on, op2 = read_block_nbt(s)
                                    if major < 7:
                                        on, op2 = _convert_nbt(on, op2, major)
                                    state_pal.append((on, op2))
                                else:
                                    on, op2 = state_pal[s.u32()]

                                if minor < 1 and leg_opac:  s.skip(4)
                                if ov_ctype == 2 or cust_col: s.skip(4)
                                if minor < 8 and has_opac:
                                    ov_alpha = s.i32()

                                overlays.append((on, op2, ov_alpha))

                        # ── biome ────────────────────────────────────────────
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

                        # ── render pixel ─────────────────────────────────────
                        # mirrors: output[z][x] = color
                        # z = row (north-south), x = column (east-west)
                        bcolors = biome_colors.get(biome_name or 'plains') \
                               or biome_colors.get('plains', {})
                        r, g, b, a = _get_color(name, props, bcolors, block_colors)

                        if overlays:
                            ar, ag, ab = r, g, b
                            for on, op2, ov_alpha in overlays:
                                or_, og, ob, oa = _get_color(on, op2, bcolors, block_colors)
                                m = oa / 255.0
                                ar += int(m * or_)
                                ag += int(m * og)
                                ab += int(m * ob)
                            n_total = len(overlays) + 1
                            r = ar // n_total
                            g = ag // n_total
                            b = ab // n_total

                        ax = px | (cx << 4) | (tile_x << 6)
                        az = pz | (cz << 4) | (tile_z << 6)
                        pix[ax, az] = (r, g, b, 255)

                # chunk metadata (after all 16x16 pixels)
                if minor >= 4: s.skip(1)   # chunkInterpretationVersion
                if minor >= 6:
                    s.skip(4)              # caveStart
                    if minor >= 7: s.skip(1)  # caveDepth

    return img


# ─── Entry point ──────────────────────────────────────────────────────────────

def render(zip_path: str, cpp_path: str, out_path: str):
    print(f"Loading lookups from {cpp_path}...")
    block_colors, biome_colors = load_lookups(cpp_path)
    print(f"  {len(block_colors)} block types, {len(biome_colors)} biomes")

    print(f"Parsing {zip_path}...")
    with zipfile.ZipFile(zip_path) as zf:
        data = zf.read('region.xaero')

    img = parse_and_render(data, block_colors, biome_colors)
    img.save(out_path)
    print(f"Saved {out_path}")


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(f'Usage: {sys.argv[0]} <region.zip> <BlockLookups.cpp> <output.png>')
    render(sys.argv[1], sys.argv[2], sys.argv[3])
