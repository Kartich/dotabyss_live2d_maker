from pathlib import Path
import sys
import catalog_downloader as c

rid = sys.argv[1] if len(sys.argv) > 1 else 'bgm0024'
version = sys.argv[2] if len(sys.argv) > 2 else ''
if not version:
    raise SystemExit('用法: python verify_bgm_download.py bgm0024 8374')
c.BASE_URL = c.build_base_url(version)
for name, kind in c.find_bgm_pair(rid, Path(c.DEFAULT_CATALOG)):
    print(kind, name)
    print(c.make_url(name))
