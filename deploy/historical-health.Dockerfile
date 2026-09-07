# Preserve the exact running kg-hub business image; overlay the status helper only.
FROM kg-hub-server:recovery-base-c969eac675d5
COPY dashboard_status.py /app/dashboard_status.py
RUN python -c "import ast,pathlib; ast.parse(pathlib.Path('/app/dashboard_status.py').read_text())"
