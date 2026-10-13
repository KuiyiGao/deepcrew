import sys

if sys.version_info < (3, 11):
    sys.exit(f"deepcrew needs Python 3.11 or newer; this is Python {sys.version.split()[0]}")

from .cli import main

sys.exit(main())
