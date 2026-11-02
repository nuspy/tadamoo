import sys

if len(sys.argv) > 1 and sys.argv[1] == "session":
    from tadamoo.session import main
    sys.exit(main(sys.argv[2:]))

if len(sys.argv) > 1 and sys.argv[1] == "acp":
    from tadamoo.acp_agent import main
    sys.exit(main(sys.argv[2:]))

from tadamoo.headless import main

sys.exit(main())
