ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY tools/export_gateway_usage.py /app/tools/export_gateway_usage.py
RUN python -c "import ast,pathlib; ast.parse(pathlib.Path('/app/tools/export_gateway_usage.py').read_text())"
