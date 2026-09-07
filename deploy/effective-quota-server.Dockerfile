ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY topology.py /app/topology.py
RUN python -c "import ast,pathlib; ast.parse(pathlib.Path('/app/topology.py').read_text())"
