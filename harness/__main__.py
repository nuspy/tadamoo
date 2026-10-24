import sys

if len(sys.argv) > 1 and sys.argv[1] == "session":
    from harness.session import main
    sys.exit(main(sys.argv[2:]))

from harness.headless import main

sys.exit(main())
