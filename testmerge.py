from merge_xaero import parse_region, merge_copy, write_region

import zipfile

def merge_zips(base_path: str, patch_path: str, output_path: str):
    with zipfile.ZipFile(base_path) as zf:
        base_data = zf.read("region.xaero")
    with zipfile.ZipFile(patch_path) as zf:
        patch_data = zf.read("region.xaero")
    base  = parse_region(base_data)
    patch = parse_region(patch_data)
    merge_copy(base, patch)
    write_region(base, output_path)

if __name__ == "__main__":
    merge_zips("21_29_og.zip", "21_29_new.zip", "21_29_m.zip")
    print("COMPLETE")