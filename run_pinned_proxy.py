"""Start the real Paritok proxy with pin-on-expand installed.

    python run_pinned_proxy.py --port 8081 --anthropic-url http://127.0.0.1:9100
"""

from __future__ import annotations

import sys

from paritok_adaptive import install

if __name__ == "__main__":
    install()
    from paritok.cli import main
    sys.argv = ["paritok", "proxy", *sys.argv[1:]]
    main()
