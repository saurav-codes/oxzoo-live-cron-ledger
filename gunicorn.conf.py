# Read by gunicorn from the working directory, so the detected start
# command stays as ox proposes it.
workers = 2
timeout = 30
accesslog = "-"
# gunicorn 26 opens a control socket under $HOME, which ox keeps read-only.
control_socket_disable = True
