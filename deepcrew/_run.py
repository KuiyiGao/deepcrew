import os
import sys

sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from deepcrew.member import main

if __name__ == "__main__":
    sys.exit(main())
