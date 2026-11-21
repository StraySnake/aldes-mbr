"""State shared between modules, set by app.main()."""

bridge = None        # upstream.UpstreamBridge, once created
ssl_context = None   # ssl.SSLContext offered to the box
commands = None      # commands.CommandTracker
box_clock_set = None  # True/False from the box's last SAS token, None = not seen yet
