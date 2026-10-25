"""支持 `python -m intellect` 方式运行。"""

from intellect.app import main

if __name__ == "__main__":
    raise SystemExit(main())
