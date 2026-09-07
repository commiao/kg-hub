FROM kg-hub-server:dashboard-health-base-20260907
COPY topology.py kg_hub_server.py dashboard_status.py /app/
RUN python -c "import ast,pathlib; [ast.parse(pathlib.Path('/app', n).read_text()) for n in ('topology.py','kg_hub_server.py','dashboard_status.py')]"
