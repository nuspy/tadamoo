import sys

if len(sys.argv) > 1 and sys.argv[1] == "session":
    from harness.session import main
    sys.exit(main(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == "acp":
    from harness.acp_agent import main
    sys.exit(main(sys.argv[2:]))

from harness.headless import main

sys.exit(main())
