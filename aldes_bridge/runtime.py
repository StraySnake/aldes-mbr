"""State shared between modules, set by app.main()."""

bridge = None        # upstream.UpstreamBridge, once created
ssl_context = None   # ssl.SSLContext offered to the box
commands = None      # commands.CommandTracker
