# -*- coding: utf-8 -*-
from pathlib import Path
from collections import Counter
from catalog_downloader import extract_bundle_names
from resource_classifier import classify_bundle

CATALOG = Path(__file__).resolve().parent / 'catalog_1.bin'

def main():
    names = extract_bundle_names(CATALOG)
    counts = Counter()
    bad=[]
    for name in names:
        info=classify_bundle(name)
        counts[info.kind]+=1
        if info.kind=='other': bad.append(name)
    print(f'[CLASSIFY] total={len(names)} categories={len(counts)} unclassified={len(bad)}')
    for k,v in sorted(counts.items()): print(f'  {k:24s} {v}')
    if bad:
        print('\n[UNCLASSIFIED]')
        for x in bad[:100]: print(x)
        return 2
    return 0
if __name__=='__main__': raise SystemExit(main())
