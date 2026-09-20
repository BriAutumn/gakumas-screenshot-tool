"""Apply explicit unresolved-field image readings to a new library copy.

python tools/import_vision_fields.py SOURCE_LIBRARY NEW_LIBRARY RESULTS_JSON ...
"""
import sys
from import_vision import apply

if __name__=='__main__':
    if len(sys.argv)<4:raise SystemExit(__doc__)
    apply(sys.argv[1],sys.argv[2],sys.argv[3:],field_mode=True)
