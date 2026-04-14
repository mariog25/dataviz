from jupyter_server.auth import passwd

p = passwd("jupyter")

with open("/root/.jupyter/jupyter_server_config.py", "w") as f:
    f.write(f"c.ServerApp.password = u'{p}'\n")
    f.write("c.ServerApp.ip = '0.0.0.0'\n")
    f.write("c.ServerApp.open_browser = False\n")
    f.write("c.ServerApp.root_dir = '/notebooks'\n")